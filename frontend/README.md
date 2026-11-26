# AUPP Reference Desk

React frontend for the AUPP RAG chatbot. Talks to the FastAPI backend
(`build_backend.py`) - retrieval, reranking, and generation all happen
server-side; this is purely the chat UI.

## Setup

```bash
npm install
```

By default the app calls the backend at `http://localhost:8000`. To point
at a different address, create a `.env` file:

```
VITE_API_BASE_URL=http://localhost:8000
```

## Run

**1. Start the backend first** (in the `aupp-rag` project directory):
```bash
python build_backend.py
```
Wait for `✅ All models loaded, API ready to serve requests` in its console -
the UI's status pill will show "Loading models" until then, and requests
sent before that will be rejected with a 503.

**2. Start the frontend:**
```bash
npm run dev
```
Open `http://localhost:5173`.

## What's here

- `src/App.jsx` - top-level state: messages, session id (persisted to
  `localStorage`), backend health polling
- `src/api.js` - thin fetch wrapper matching `build_backend.py`'s
  `/health`, `/chat`, `/session/{id}` endpoints exactly
- `src/components/ChatMessage.jsx` - renders both user and assistant turns;
  assistant turns include the ranked source cards
- `src/components/SourceCard.jsx` - a single citation, styled as a library
  index card. The rank number is the chunk's real position after
  reranking, not decorative
- `src/components/ChatInput.jsx` - the input bar (Enter to send, Shift+Enter
  for a new line)
- `src/index.css` - the full design token system and styles (see the
  `:root` block for the color/type tokens)

## Notes

- Session history is server-side, in-memory, per `session_id` - it does not
  survive a backend restart. The frontend will keep using its stored
  session id, which will just start a fresh empty history server-side.
- CORS is wide open (`allow_origins=["*"]`) on the backend for local dev -
  tighten this before deploying anywhere real.
- `npm run build` produces a static `dist/` folder you can serve from
  anywhere (nginx, Vercel, GitHub Pages, etc.) once the backend has a
  real, reachable URL instead of `localhost`.
