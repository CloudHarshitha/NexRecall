import os
import glob
from typing import List, Dict, Any
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer
import chromadb
from chromadb.config import Settings

def generate_dummy_transcripts(data_dir: str = "data/transcripts") -> None:
    """
    Generates dummy meeting transcripts with distinct action items for testing the RAG system.
    """
    os.makedirs(data_dir, exist_ok=True)
    
    transcript_1 = """Meeting: Project Architecture Review
Date: 2026-10-01
Attendees: Alice, Bob, Charlie

Alice: Let's discuss the backend architecture for the Omni-Meeting Memory Agent.
Bob: We decided to use ChromaDB for our local vector store because of its lightweight nature and fast similarity search capabilities.
Charlie: That sounds good. What about the embedding model?
Alice: We'll go with all-MiniLM-L6-v2. It creates 384-dimensional dense vectors which are highly optimized for semantic similarity tasks.
Bob: Great. I'll take the action item to set up the ChromaDB instance.
Alice: And Charlie, can you write the text chunking logic?
Charlie: Action item assigned: I will implement the RecursiveCharacterTextSplitter by next Tuesday.
"""
    
    transcript_2 = """Meeting: Frontend UI Sync
Date: 2026-10-03
Attendees: Alice, Dave, Eve

Alice: How is the Chainlit UI coming along?
Dave: I've created the basic chat interface, but we need to integrate it with the langgraph orchestrator.
Eve: I can handle the langgraph integration. The state machine needs to route correctly between the task extractor and history synthesizer.
Alice: Perfect. Dave, please ensure the UI can handle markdown formatting.
Dave: Action item confirmed: I will add markdown support to the Chainlit interface by Friday.
Eve: My action item is to finish the langgraph integration by Thursday.
"""
    
    with open(os.path.join(data_dir, "meeting_1.txt"), "w", encoding="utf-8") as f:
        f.write(transcript_1)
        
    with open(os.path.join(data_dir, "meeting_2.txt"), "w", encoding="utf-8") as f:
        f.write(transcript_2)
        
    print(f"Generated dummy transcripts in '{data_dir}'.")

def ingest_data(data_dir: str = "data/transcripts", collection_name: str = "meeting_memory") -> None:
    """
    Ingests text transcripts, chunks them, generates embeddings, and stores them in ChromaDB.
    """
    # 1. Initialize the Embedding Model
    try:
        print("Initializing embedding model (all-MiniLM-L6-v2)...")
        # all-MiniLM-L6-v2 maps text into a 384-dimensional dense vector space.
        # This mapping captures semantic meaning, meaning texts with similar contexts 
        # will have a smaller angle between their respective vectors (measured via cosine similarity).
        embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        print(f"Error initializing SentenceTransformer: {e}")
        return

    # 2. Initialize ChromaDB
    try:
        print(f"Initializing ChromaDB persistent client. Target collection: '{collection_name}'...")
        client = chromadb.PersistentClient(path="./chroma_db")
        # We use a persistent collection to ensure embeddings are stored to disk for later retrieval.
        collection = client.get_or_create_collection(name=collection_name)
    except Exception as e:
        print(f"Error initializing ChromaDB: {e}")
        return

    # 3. Configure the Text Splitter
    # The chunking logic is critical in RAG (Retrieval-Augmented Generation).
    # A chunk_size of 500 characters balances granularity (finding specific facts) and context (giving the LLM enough surrounding info).
    # The chunk_overlap of 50 characters prevents context loss at the boundaries of chunks,
    # ensuring that a sentence split down the middle retains its semantic continuity across adjacent chunks.
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        length_function=len,
        is_separator_regex=False,
    )
    
    if not os.path.exists(data_dir):
        print(f"Directory {data_dir} not found. Generating dummy data...")
        generate_dummy_transcripts(data_dir)
        
    files = glob.glob(os.path.join(data_dir, "*.txt"))
    if not files:
        print(f"No .txt files found in {data_dir}. Generating dummy data...")
        generate_dummy_transcripts(data_dir)
        files = glob.glob(os.path.join(data_dir, "*.txt"))

    documents: List[str] = []
    metadatas: List[Dict[str, Any]] = []
    ids: List[str] = []
    
    doc_id = 0
    for file_path in files:
        print(f"Processing '{file_path}'...")
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                text = f.read()
            
            # Split the document text into smaller chunks
            chunks = text_splitter.split_text(text)
            file_name = os.path.basename(file_path)
            
            for i, chunk in enumerate(chunks):
                documents.append(chunk)
                # Attaching metadata allows for future filtering (e.g., querying only specific files or dates)
                metadatas.append({"file": file_name, "chunk_index": i})
                ids.append(f"doc_{doc_id}_chunk_{i}")
                
            doc_id += 1
        except Exception as e:
            print(f"Error processing file '{file_path}': {e}")
        
    if not documents:
        print("No text chunks generated. Aborting ingestion.")
        return
        
    # 4. Generate Embeddings
    try:
        print(f"Generating embeddings for {len(documents)} chunks...")
        # The encode method converts our textual chunks into a list of mathematical vectors (floats).
        embeddings = embedding_model.encode(documents).tolist()
    except Exception as e:
        print(f"Error generating embeddings: {e}")
        return
    
    # 5. Insert into Vector Database
    try:
        print(f"Upserting vectors into collection '{collection_name}'...")
        # ChromaDB utilizes an underlying index (like HNSW) to efficiently organize these high-dimensional vectors,
        # allowing for O(log N) approximate nearest neighbor (ANN) lookups during retrieval.
        collection.add(
            documents=documents,
            embeddings=embeddings,
            metadatas=metadatas,
            ids=ids
        )
        print("Ingestion complete. Data is now ready for retrieval.")
    except Exception as e:
        print(f"Error adding documents to ChromaDB: {e}")

if __name__ == "__main__":
    ingest_data()
