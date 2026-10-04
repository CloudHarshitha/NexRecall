import os
import glob
from langchain_text_splitters import RecursiveCharacterTextSplitter
from sentence_transformers import SentenceTransformer
import chromadb

def ingest_data(data_dir: str = "data", collection_name: str = "meeting_transcripts"):
    print("Initializing embedding model...")
    embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    
    print("Initializing ChromaDB...")
    client = chromadb.PersistentClient(path="./chroma_db")
    collection = client.get_or_create_collection(name=collection_name)
    
    text_splitter = RecursiveCharacterTextSplitter(
        chunk_size=500,
        chunk_overlap=50,
        length_function=len,
        is_separator_regex=False,
    )
    
    if not os.path.exists(data_dir):
        os.makedirs(data_dir)
        print(f"Created data directory at {data_dir}. Please add .txt transcripts and run again.")
        return
        
    files = glob.glob(os.path.join(data_dir, "*.txt"))
    if not files:
        print(f"No .txt files found in {data_dir}.")
        return

    documents = []
    metadatas = []
    ids = []
    
    doc_id = 0
    for file_path in files:
        print(f"Processing {file_path}...")
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
        
        chunks = text_splitter.split_text(text)
        file_name = os.path.basename(file_path)
        
        for i, chunk in enumerate(chunks):
            documents.append(chunk)
            metadatas.append({"file": file_name, "date": "unknown"})
            ids.append(f"doc_{doc_id}_{i}")
            
        doc_id += 1
        
    if not documents:
        print("No text chunks generated.")
        return
        
    print(f"Generating embeddings for {len(documents)} chunks...")
    embeddings = embedding_model.encode(documents).tolist()
    
    print("Adding to vector database...")
    collection.add(
        documents=documents,
        embeddings=embeddings,
        metadatas=metadatas,
        ids=ids
    )
    print("Ingestion complete.")

if __name__ == "__main__":
    ingest_data()
