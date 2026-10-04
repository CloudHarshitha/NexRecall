import json
from retriever import retrieve_meeting_context

def run_tests():
    print("==================================================")
    print("Testing Vector Retrieval System")
    print("==================================================")
    
    query = "What were the action items?"
    print(f"\nQuery: '{query}'")
    print("Searching vector database...\n")
    
    # Perform the retrieval
    results = retrieve_meeting_context(query=query, top_k=3)
    
    # Output formatting for verification
    if not results:
        print("No results found or an error occurred. Have you run ingest.py first?")
        return
        
    for i, res in enumerate(results):
        print(f"--- Result {i+1} ---")
        print(f"Metadata: {json.dumps(res['metadata'], indent=2)}")
        print(f"Text Snippet:\n{res['text']}")
        print("-" * 50)
        
    print(f"\nSuccessfully retrieved {len(results)} relevant chunks.")

if __name__ == "__main__":
    run_tests()
