# NexRecall — Intelligent Meeting Recall Assistant

## 1. Problem Statement

Meeting participants often find it difficult to remember important discussions, decisions, and action items after a meeting has ended. Important information can get lost in lengthy transcripts, making it harder to track responsibilities and follow up on pending tasks.

NexRecall aims to address this problem by helping users revisit meeting information and identify key takeaways from meeting transcripts or text.

## 2. Objectives

1. Accept meeting transcripts or text as input.
2. Help users understand the key points discussed during a meeting.
3. Identify and present important decisions.
4. Extract action items and tasks from meeting content.
5. Support reminders and follow-ups for relevant action items.
6. Make important meeting information easier to revisit.

## 3. Architecture

```text
       Meeting Transcript / Text
                  |
                  v
          Input Processing
                  |
                  v
        Meeting Content Analysis
                  |
          +-------+-------+
          |       |       |
          v       v       v
       Summary Decisions Actions
          |       |       |
          +-------+-------+
                  |
                  v
       Meeting Information View
                  |
                  v
       Reminders / Follow-ups
```

*Note: This is a conceptual architecture. The actual implementation may differ.*

## 4. Technologies

| Component | Technology |
|---|---|
| Frontend | html/css|
| Backend | python |
| Database | supabase |
| Text Processing | text-embedding-3-small |
| AI / Language Model | Nexus AI |
| Development Tools | Kiro, Antigravity, Git |

## 5. Key Features

- **Meeting Summaries:** Present the main points from meeting transcripts.
- **Decision Identification:** Highlight important decisions recorded in the meeting content.
- **Action Item Extraction:** Identify tasks and follow-up activities mentioned in discussions.
- **Reminders:** Support follow-up reminders for relevant tasks.
- **Meeting Recall:** Help users revisit important meeting information.

## 6. Installation and Setup

### Prerequisites

- A code editor.
- Git, if the project is managed using Git.
- The software and dependencies required by the selected technology stack.

### Steps

1. Clone the project repository:

   ```bash
   git clone .com/CloudHarshitha/NexRecall.git)
   ```



3. Install the project dependencies according to the project's configuration.

4. Configure any required environment variables or API keys.

5. Start the application using the command specified by the project setup.

*Note: Replace these general instructions with the exact installation and run commands after confirming the project's configuration.*

## 7. Project Workflow

1. The user provides a meeting transcript or text.
2. The system processes the supplied content.
3. The system identifies key information, including summaries, decisions, and action items.
4. The results are presented to the user.
5. Relevant action items can support reminders and follow-ups.

## 8. Team Members

Harshitha SB
Chiruvelli Sai Sri

## 9. Future Enhancements

Potential enhancements will depend on the final project scope and implementation.

## 10. Project Purpose

NexRecall is a project focused on making meeting information easier to understand, remember, and act upon.
