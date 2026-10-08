"""
process_upload.py - Upload Processing Pipeline for NexRecall

This module handles:
1. Text transcript uploads (.txt, .srt, .vtt, .md)
2. Video uploads (.mp4, .webm, .mkv, .avi, .mov) → audio extraction → transcription
3. Chunking, embedding, and storing in ChromaDB
4. Generating Minutes of Meeting (MoM) summaries using the LLM
"""

import os
import re
import tempfile
from datetime import datetime
from typing import Optional, List, Dict, Any

from langchain_text_splitters import RecursiveCharacterTextSplitter
import chromadb
from langchain_openai import ChatOpenAI
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_core.messages import HumanMessage
from dotenv import load_dotenv

load_dotenv()

# Global caches for lazy loading
_embedding_model = None
_chroma_collection = None
_llm = None
_text_splitter = None

def get_pipeline_components():
    global _embedding_model, _chroma_collection, _llm, _text_splitter
    
    api_key = os.environ.get("NEXUS_API_KEY", "default-key")
    base_url = os.environ.get("NEXUS_API_BASE_URL", "https://nexusapi.navigatelabs.ai")
    
    if _embedding_model is None:
        api_key = os.environ.get("NEXUS_API_KEY", "default-key")
        # Google's native embedding API — the OpenAI-compatible endpoint does
        # not support embeddings (returns 501), so we use the native SDK.
        _embedding_model = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001",
            google_api_key=api_key,
        )
        
    if _llm is None:
        llm_primary = ChatOpenAI(model="gemini-3.5-flash", api_key=api_key, base_url=base_url, max_retries=3)
        llm_fallback_1 = ChatOpenAI(model="gemini-3.6-flash", api_key=api_key, base_url=base_url, max_retries=3)
        llm_fallback_2 = ChatOpenAI(model="gemini-3.8-flash", api_key=api_key, base_url=base_url, max_retries=3)
        _llm = llm_primary.with_fallbacks([llm_fallback_1, llm_fallback_2])
        
    if _chroma_collection is None:
        client = chromadb.PersistentClient(path="./chroma_db")
        _chroma_collection = client.get_or_create_collection(name="meeting_memory")
        
    if _text_splitter is None:
        _text_splitter = RecursiveCharacterTextSplitter(
            chunk_size=500,
            chunk_overlap=50,
            length_function=len,
            is_separator_regex=False,
        )
        
    return _embedding_model, _chroma_collection, _llm, _text_splitter



# ==============================================================================
# Subtitle Parsing (SRT / VTT)
# ==============================================================================

def parse_srt(content: str) -> str:
    """
    Parse an SRT subtitle file and extract only the dialogue text.
    Strips sequence numbers, timestamps, and formatting tags.
    """
    # Remove sequence numbers (lines that are just digits)
    lines = content.strip().split('\n')
    text_lines = []
    for line in lines:
        line = line.strip()
        # Skip empty lines, sequence numbers, and timestamp lines
        if not line:
            continue
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'\d{2}:\d{2}:\d{2}', line):
            continue
        # Remove HTML-like tags (e.g., <i>, <b>)
        line = re.sub(r'<[^>]+>', '', line)
        text_lines.append(line)
    return ' '.join(text_lines)


def parse_vtt(content: str) -> str:
    """
    Parse a WebVTT subtitle file and extract only the dialogue text.
    Strips the WEBVTT header, timestamps, and formatting tags.
    """
    lines = content.strip().split('\n')
    text_lines = []
    for line in lines:
        line = line.strip()
        # Skip WEBVTT header, empty lines, and timestamp lines
        if not line or line.startswith('WEBVTT') or line.startswith('NOTE'):
            continue
        if re.match(r'\d{2}:\d{2}', line):
            continue
        # Skip cue identifiers (lines that are just identifiers before timestamps)
        if re.match(r'^[\w-]+$', line) and '-->' not in line:
            continue
        line = re.sub(r'<[^>]+>', '', line)
        text_lines.append(line)
    return ' '.join(text_lines)


# ==============================================================================
# Video Transcription
# ==============================================================================

def transcribe_video(video_path: str, progress_callback=None) -> Optional[str]:
    """
    Extract audio from a video file and transcribe it using OpenAI Whisper API.
    Replaces the heavy local Whisper model to save memory.
    """
    audio_path = None
    try:
        from moviepy import VideoFileClip
        import openai
        
        audio_path = os.path.join(tempfile.gettempdir(), "nexrecall_audio.mp3")
        
        # Step 1: Extract audio as a compressed format (mp3) to save bandwidth
        video = VideoFileClip(video_path)
        video.audio.write_audiofile(
            audio_path,
            logger=None
        )
        video.close()
        
        # Step 2: Transcribe using the API instead of a local PyTorch model
        api_key = os.environ.get("NEXUS_API_KEY", "default-key")
        base_url = os.environ.get("NEXUS_API_BASE_URL", "https://nexusapi.navigatelabs.ai")
        client = openai.OpenAI(api_key=api_key, base_url=base_url)
        
        with open(audio_path, "rb") as audio_file:
            transcript = client.audio.transcriptions.create(
                model="whisper-1", 
                file=audio_file
            )
        
        return transcript.text
        
    except ImportError as e:
        print(f"Missing dependency for video transcription: {e}")
        print("Install with: pip install moviepy openai")
        return None
    except Exception as e:
        print(f"Video transcription error: {e}")
        return None
    finally:
        if audio_path and os.path.exists(audio_path):
            try:
                os.remove(audio_path)
            except OSError:
                pass


# ==============================================================================
# Core Processing Pipeline
# ==============================================================================

def process_transcript(text: str, meeting_name: str, user_id: str) -> str:
    """
    Process a text transcript through the full RAG ingestion pipeline:
    1. Chunk the text using RecursiveCharacterTextSplitter
    2. Generate embeddings with SentenceTransformer
    3. Store chunks + embeddings in ChromaDB
    4. Generate Minutes of Meeting (MoM) using the LLM
    
    Args:
        text: The full transcript text
        meeting_name: A name/identifier for this meeting
        user_id: The ID of the user uploading the meeting
        
    Returns:
        The generated Minutes of Meeting as a formatted string
    """
    # Lazy load components
    embed_model, col, llm_model, splitter = get_pipeline_components()
    
    # Chunk the text
    chunks = splitter.split_text(text)
    
    if not chunks:
        return "⚠️ No meaningful content found in the transcript."
    
    # Generate embeddings
    embeddings = embed_model.embed_documents(chunks)
    
    # Create unique IDs and metadata for each chunk (deterministic to avoid duplicates on re-upload)
    today = datetime.now().strftime("%Y-%m-%d")
    ids = [f"{user_id}_{meeting_name}_chunk_{i}" for i in range(len(chunks))]
    metadatas = [
        {
            "file": meeting_name,
            "chunk_index": i,
            "date": today,
            "user_id": user_id
        }
        for i in range(len(chunks))
    ]
    
    # Store in ChromaDB (upsert to handle re-uploads gracefully)
    col.upsert(
        documents=chunks,
        embeddings=embeddings,
        metadatas=metadatas,
        ids=ids,
    )
    print(f"[SUCCESS] Stored {len(chunks)} chunks from '{meeting_name}' in ChromaDB.")
    
    # Generate MoM
    mom = generate_mom(text, meeting_name)
    
    return mom


def generate_mom(transcript: str, meeting_name: str) -> str:
    """
    Generate comprehensive Minutes of Meeting (MoM) from transcript text
    using the configured LLM.
    
    Args:
        transcript: The full transcript text
        meeting_name: Name/identifier of the meeting
        
    Returns:
        Formatted MoM as a markdown string
    """
    # Truncate if too long for the context window
    max_chars = 10000
    truncated = transcript[:max_chars] if len(transcript) > max_chars else transcript
    was_truncated = len(transcript) > max_chars
    
    truncation_note = ""
    if was_truncated:
        truncation_note = "\n\n> Note: The transcript was truncated for summary generation, but the full text has been stored for chat retrieval."

    prompt = f"""You are an expert meeting analyst. Generate comprehensive Minutes of Meeting (MoM) from the following transcript.

**Meeting: {meeting_name}**

**Transcript:**
{truncated}

**Generate the MoM in the following format:**

## 📋 Minutes of Meeting: {meeting_name}

### 📅 Meeting Details
- **Date**: [Extract from transcript or mark as "Not specified"]
- **Attendees**: [List all participants mentioned]

### 📝 Summary
[2-3 sentence overview of what was discussed in the meeting]

### 🔑 Key Discussion Points
[Bullet list of the main topics discussed]

### ✅ Action Items
| # | Action Item | Assigned To | Deadline |
|---|------------|-------------|----------|
[Extract all action items with assignees and deadlines. If no deadline mentioned, put "TBD"]

### 📌 Decisions Made
[List any decisions that were made during the meeting]

### 🔜 Next Steps
[Any follow-up items or next meeting plans]

Be thorough but concise. Extract all actionable information."""
    
    _, _, llm_model, _ = get_pipeline_components()
    response = llm_model.invoke([HumanMessage(content=prompt)])
    return response.content + truncation_note


def read_uploaded_file(file_path: str, file_name: str) -> Optional[str]:
    """
    Read and parse an uploaded file based on its extension.
    Handles .txt, .md, .srt, and .vtt formats.
    
    Args:
        file_path: Path to the uploaded file
        file_name: Original filename (used to determine format)
        
    Returns:
        Extracted text content, or None if format is unsupported
    """
    ext = os.path.splitext(file_name)[1].lower()
    
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except UnicodeDecodeError:
        # Try with latin-1 fallback
        with open(file_path, 'r', encoding='latin-1') as f:
            content = f.read()
    
    if ext in ('.txt', '.md'):
        return content
    elif ext == '.srt':
        return parse_srt(content)
    elif ext == '.vtt':
        return parse_vtt(content)
    else:
        return None
