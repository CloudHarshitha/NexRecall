import os
from typing import List, Dict, Any
import chromadb
from langchain_google_genai import GoogleGenerativeAIEmbeddings

# Global cache for lazy loading
_embedding_model = None
_collection = None

def get_retriever_components():
    global _embedding_model, _collection
    if _embedding_model is None:
        api_key = os.environ.get("NEXUS_API_KEY", "default-key")
        # Google's native embedding API — the OpenAI-compatible endpoint does
        # not support embeddings (returns 501), so we use the native SDK.
        _embedding_model = GoogleGenerativeAIEmbeddings(
            model="models/gemini-embedding-001",
            google_api_key=api_key,
        )
    if _collection is None:
        try:
            client = chromadb.PersistentClient(path="./chroma_db")
            _collection = client.get_collection(name="meeting_memory")
        except Exception as e:
            print(f"Warning: Retriever initialization failed. Check if ingest.py was run. Error: {e}")
            _collection = None
            
    return _embedding_model, _collection

def retrieve_meeting_context(query: str, user_id: str, top_k: int = 10) -> List[Dict[str, Any]]:
    """
    Core bridge between the RAG engine and the Agent UI.
    """
    embed_model, col = get_retriever_components()
    if embed_model is None or col is None:
        print("Error: Retrieval system is offline. Model or DB not initialized.")
        return []
        
    try:
        # Use API-based embedding instead of local SentenceTransformer
        query_embedding = [embed_model.embed_query(query)]
        
        results = col.query(
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
