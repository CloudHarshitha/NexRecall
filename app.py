"""
app.py - Chainlit UI for the Omni-Meeting Memory Agent (NexRecall)

Supports:
- Uploading meeting transcripts (.txt, .srt, .vtt, .md) and videos (.mp4, .webm, etc.)
- Auto-generating Minutes of Meeting (MoM) from uploads
- Chatting about past meetings using the RAG pipeline
- Auth and persistent chat history
"""

# ---------------------------------------------------------------------------
# Patch: anyio 4.x + starlette 0.47+ runs sync endpoints in worker threads
# via anyio.to_thread.run_sync → sniffio.current_async_library(). sniffio
# uses a threading.local that worker threads don't inherit, so it raises
# AsyncLibraryNotFoundError inside every sync Chainlit route (/health etc.).
#
# Fix: patch sniffio to return "asyncio" as fallback when no library is set.
# This must happen before Chainlit/fastapi boot — placing it here at the top
# of app.py works because Python executes this before any other import chain.
# ---------------------------------------------------------------------------
import sniffio as _sniffio

_orig_current_async_library = _sniffio.current_async_library

def _safe_current_async_library():
    try:
        return _orig_current_async_library()
    except _sniffio.AsyncLibraryNotFoundError:
        return "asyncio"

_sniffio.current_async_library = _safe_current_async_library
# Also patch the internal reference used by anyio
import sniffio._impl as _sniffio_impl
_sniffio_impl.current_async_library = _safe_current_async_library
# ---------------------------------------------------------------------------

import chainlit as cl
from dotenv import load_dotenv
load_dotenv()

from langchain_core.messages import HumanMessage
from agent import app_graph
import asyncio
import os
import uuid

from process_upload import process_transcript, transcribe_video, read_uploaded_file
from chainlit.data.sql_alchemy import SQLAlchemyDataLayer
from database import init_db, AsyncSessionLocal, AppUser, verify_password, Meeting
from sqlalchemy.future import select

# Register the Auth Router
import auth

# 1. Setup Data Layer for Chainlit via the proper decorator
@cl.data_layer
def get_data_layer():
    return SQLAlchemyDataLayer(conninfo="sqlite+aiosqlite:///app.db")

# Supported file extensions
SUPPORTED_TEXT_EXTS = {'.txt', '.srt', '.vtt', '.md'}
SUPPORTED_VIDEO_EXTS = {'.mp4', '.webm', '.mkv', '.avi', '.mov', '.wav', '.mp3', '.m4a'}

# ==============================================================================
# Authentication Callbacks
# ==============================================================================
@cl.password_auth_callback
async def auth_callback(email: str, password: str):
    async with AsyncSessionLocal() as session:
        stmt = select(AppUser).where(AppUser.email == email)
        result = await session.execute(stmt)
        user = result.scalar_one_or_none()
        
        if user and verify_password(password, user.password_hash):
            return cl.User(identifier=email, metadata={"id": user.id, "name": user.name, "avatar_url": user.avatar_url})
    return None

if os.environ.get("OAUTH_GOOGLE_CLIENT_ID") and os.environ.get("OAUTH_GOOGLE_CLIENT_SECRET"):
    @cl.oauth_callback
    def oauth_callback(provider_id: str, token: str, raw_user_data: dict, default_user: cl.User):
        # Only if GOOGLE OAUTH is configured
        return default_user


@cl.on_chat_start
async def on_chat_start():
    # Ensure our custom tables are created
    await init_db()
    
    # Extract user ID for scoped operations
    user = cl.user_session.get("user")
    if user:
        cl.user_session.set("id", user.metadata.get("id", user.identifier))
        # Chainlit's data layer manages thread title, but we can set it up here.
    
    welcome_message = (
        "Welcome to **NexRecall**! 🚀\n\n"
        "I can help you recall meeting history or extract action items from our past discussions.\n\n"
        "---\n\n"
        "**📤 Upload a Meeting**\n"
        "Click the 📎 icon below to upload:\n"
        "- **Transcripts**: `.txt`, `.srt`, `.vtt`, `.md`\n"
        "- **Videos/Audio**: `.mp4`, `.webm`, `.mkv`, `.wav`, `.mp3`\n\n"
        "I'll automatically generate a **Minutes of Meeting (MoM)** summary, "
        "and you can then chat with me about the meeting!\n\n"
        "---\n\n"
        "**💬 Chat About Past Meetings**\n"
        "Ask me questions like:\n"
        "- *What action items are assigned to Bob?*\n"
        "- *When was the timeline extended?*\n"
        "- *Summarize the architecture review meeting*"
    )
    await cl.Message(content=welcome_message).send()

@cl.on_chat_resume
async def on_chat_resume(thread):
    # Restore context based on resumed thread
    thread_id = thread.get("id")
    if not thread_id:
        return
        
    user = cl.user_session.get("user")
    if user:
        cl.user_session.set("id", user.metadata.get("id", user.identifier))
    
    async with AsyncSessionLocal() as session:
        stmt = select(Meeting).where(Meeting.thread_id == thread_id)
        result = await session.execute(stmt)
        meetings = result.scalars().all()
        
        if meetings:
            for meeting in meetings:
                await cl.Message(content=f"*(Restored context for meeting: `{meeting.filename}`)*\n\n---\n\n{meeting.mom_summary}").send()


@cl.on_message
async def on_message(message: cl.Message):
    # Auto-name thread from first message if not uploaded file
    if len(cl.chat_context.get()) <= 2:
        pass # Optional logic
        
    files = message.elements if message.elements else []
    
    if files:
        await handle_file_upload(message, files)
    else:
        await handle_chat(message)


# ==============================================================================
# File Upload Handler
# ==============================================================================
async def handle_file_upload(message: cl.Message, files: list):
    user_id = cl.user_session.get("id", "default_user")
    thread_id = cl.context.session.thread_id
    
    for file_element in files:
        file_name = file_element.name
        file_path = file_element.path
        ext = os.path.splitext(file_name)[1].lower()
        meeting_name = os.path.splitext(file_name)[0]

        if ext in SUPPORTED_TEXT_EXTS:
            status_msg = cl.Message(content=f"📄 **Uploading transcript:** `{file_name}`\n\n⏳ Reading and processing...")
            await status_msg.send()

            text = await cl.make_async(read_uploaded_file)(file_path, file_name)
            if not text or not text.strip():
                await cl.Message(content=f"⚠️ The file `{file_name}` appears to be empty or could not be parsed.").send()
                return

            async with cl.Step(name="Processing Transcript", type="tool") as step:
                step.input = f"Chunking, embedding, and storing {len(text)} characters from '{file_name}'..."
                mom = await cl.make_async(process_transcript)(text, meeting_name, user_id)
                step.output = f"✅ Stored in vector database. MoM generated."

            # Save meeting to DB
            async with AsyncSessionLocal() as session:
                new_meeting = Meeting(id=str(uuid.uuid4()), user_id=user_id, thread_id=thread_id, filename=file_name, transcript=text, mom_summary=mom)
                session.add(new_meeting)
                await session.commit()

            await cl.Message(content=f"✅ **Meeting processed successfully!**\n\n📊 *{len(text):,} characters ingested from `{file_name}`*\n\n---\n\n{mom}\n\n---\n\n💬 You can now ask me questions about this meeting!").send()

        elif ext in SUPPORTED_VIDEO_EXTS:
            status_msg = cl.Message(content=f"🎬 **Uploading {'video' if ext in {'.mp4','.webm','.mkv','.avi','.mov'} else 'audio'}:** `{file_name}`\n\n⏳ Extracting audio and transcribing using Whisper AI...\nThis may take a few minutes depending on the file length.")
            await status_msg.send()

            async with cl.Step(name="Transcribing Audio", type="tool") as step:
                step.input = f"Extracting audio from '{file_name}' and running Whisper speech-to-text..."
                transcript = await cl.make_async(transcribe_video)(file_path)
                if transcript is None:
                    step.output = "❌ Transcription failed."
                    await cl.Message(content="❌ **Could not transcribe the video/audio file.**\n\nPossible reasons:\n- The file might be corrupted or in an unsupported format\n- Missing dependencies (`moviepy`, `transformers`)\n\n**Alternative:** Upload a text transcript (`.txt` or `.srt`) instead.").send()
                    return
                step.output = f"✅ Transcribed {len(transcript):,} characters of text."

            async with cl.Step(name="Raw Transcript", type="tool") as step:
                step.input = "Extracted transcript text"
                preview = transcript[:1000] + ("..." if len(transcript) > 1000 else "")
                step.output = preview

            async with cl.Step(name="Generating Minutes of Meeting", type="tool") as step:
                step.input = f"Chunking, embedding, and generating MoM for '{meeting_name}'..."
                mom = await cl.make_async(process_transcript)(transcript, meeting_name, user_id)
                step.output = "✅ MoM generated and meeting stored in database."

            async with AsyncSessionLocal() as session:
                new_meeting = Meeting(id=str(uuid.uuid4()), user_id=user_id, thread_id=thread_id, filename=file_name, transcript=transcript, mom_summary=mom)
                session.add(new_meeting)
                await session.commit()

            await cl.Message(content=f"✅ **Video processed successfully!**\n\n🎙️ *Transcribed {len(transcript):,} characters from `{file_name}`*\n\n---\n\n{mom}\n\n---\n\n💬 You can now ask me questions about this meeting!").send()

        else:
            supported = ', '.join(f'`{e}`' for e in sorted(SUPPORTED_TEXT_EXTS | SUPPORTED_VIDEO_EXTS))
            await cl.Message(content=f"❌ **Unsupported file type:** `{ext}`\n\nSupported formats: {supported}").send()


# ==============================================================================
# Chat Handler (RAG Pipeline)
# ==============================================================================
async def handle_chat(message: cl.Message):
    user_id = cl.user_session.get("id", "default_user")
    msg = cl.Message(content="")

    async with cl.Step(name="Agent Reasoning", type="tool") as step:
        step.input = f"Processing query: '{message.content}'"
        inputs = {"messages": [HumanMessage(content=message.content)], "user_id": user_id}
        result = await app_graph.ainvoke(inputs)
        intent = result.get("intent", "Unknown")
        step.output = f"Graph execution complete. Routed as: **{intent}**"

    final_response = result.get("final_response", "Sorry, I could not process that request.")

    for chunk in final_response.split(" "):
        await msg.stream_token(chunk + " ")
        await asyncio.sleep(0.02)

    await msg.update()
