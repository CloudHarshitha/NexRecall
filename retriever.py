import chromadb
from sentence_transformers import SentenceTransformer
from typing import List, Dict, Any

# ==============================================================================
# Initialization Block
# ==============================================================================
# We initialize the embedding model and vector database connection globally.
# This prevents reloading the model (which is I/O and computationally heavy) 
# on every single query request, significantly reducing latency.
try:
    embedding_model = SentenceTransformer("all-MiniLM-L6-v2")
    client = chromadb.PersistentClient(path="./chroma_db")
    # We target the 'meeting_memory' collection created during the ingestion phase.
    collection = client.get_collection(name="meeting_memory")
except Exception as e:
    print(f"Warning: Retriever initialization failed. Check if ingest.py was run. Error: {e}")
    embedding_model = None
    collection = None

def retrieve_meeting_context(query: str, user_id: str, top_k: int = 10) -> List[Dict[str, Any]]:
    """
    Core bridge between the RAG engine and the Agent UI.
    
    This function processes a natural language query, projects it into the same
    vector space as our document chunks, and retrieves the most semantically 
    relevant context to assist the LLM in generation.

    Args:
        query (str): The user's input question or statement.
        user_id (str): The ID of the current user.
        top_k (int, optional): The number of top matching chunks to return. Defaults to 3.

    Returns:
        List[Dict[str, Any]]: A list of dictionaries containing 'text' (the chunk) 
                              and 'metadata' (source file, date, etc.).
    """
    if embedding_model is None or collection is None:
        print("Error: Retrieval system is offline. Model or DB not initialized.")
        return []
        
    try:
        # 1. Query Vectorization
        # The query is passed through the exact same SentenceTransformer model used during ingestion.
        # This maps the user's query into the same 384-dimensional hyperspace as the document chunks.
        query_embedding = embedding_model.encode([query]).tolist()
        
        # 2. Similarity Search (Vector Distance Calculation)
        # We query ChromaDB with our query vector. The database calculates the distance
        # between the query vector and the stored document vectors. 
        # Typically, this relies on Cosine Similarity or L2 distance.
        # Cosine Similarity focuses on the angle between vectors (ignoring magnitude),
        # which is highly effective for determining semantic equivalence regardless of text length.
        results = collection.query(
            query_embeddings=query_embedding,
            n_results=top_k,
            where={"user_id": user_id}
        )
        
        # 3. Format the Results
        output = []
        if results and 'documents' in results and results['documents'][0]:
            # ChromaDB returns lists of lists (to support batch queries).
            # We take the first index [0] since we only submitted one query.
            docs = results['documents'][0]
            metas = results['metadatas'][0]
            
            for doc, meta in zip(docs, metas):
                output.append({
                    "text": doc,
                    "metadata": meta
                })
                
        return output

    except Exception as e:
        print(f"Error during retrieval process: {e}")
        return []
