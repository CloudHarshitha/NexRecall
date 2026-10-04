import chromadb
from sentence_transformers import SentenceTransformer

# Initialize these globally so they are ready for inference
try:
    embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path="./chroma_db")
    collection = client.get_collection(name="meeting_transcripts")
except Exception as e:
    print(f"Warning: Initialization failed (DB might be empty). {e}")
    embedding_model = None
    collection = None

def retrieve_meeting_context(query: str, top_k: int = 3) -> list[dict]:
    """
    Core bridge between the RAG engine and the Agent UI.
    Input: User query string.
    Output: List of dictionaries with 'text' (chunk) and 'metadata' (date, file).
    """
    if embedding_model is None or collection is None:
        print("Warning: retriever components not initialized properly.")
        return []
        
    # Generate embedding for the query
    query_embedding = embedding_model.encode([query]).tolist()
    
    # Query ChromaDB
    results = collection.query(
        query_embeddings=query_embedding,
        n_results=top_k
    )
    
    output = []
    if results and 'documents' in results and results['documents'][0]:
        docs = results['documents'][0]
        metas = results['metadatas'][0]
        for doc, meta in zip(docs, metas):
            output.append({
                "text": doc,
                "metadata": meta
            })
            
    return output
