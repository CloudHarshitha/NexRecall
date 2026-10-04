def retrieve_meeting_context(query: str, top_k: int = 3) -> list[dict]:
    """
    Mock database function to retrieve meeting context based on a query.
    Returns a static mock list of dictionaries containing text and metadata.
    This acts as a temporary bridge until the backend branch is merged.
    """
    return [
        {
            "text": "We decided to migrate the API to the new v2 endpoints to improve performance.",
            "metadata": {"date": "2026-10-01", "speaker": "Alice"}
        },
        {
            "text": "Bob is assigned to update the documentation by next Friday.",
            "metadata": {"date": "2026-10-02", "speaker": "Bob"}
        },
        {
            "text": "The timeline for the Omni-Meeting Memory Agent project is extended to November.",
            "metadata": {"date": "2026-10-03", "speaker": "Charlie"}
        }
    ]
