"use client";

import Link from "next/link";
import { useEffect, useRef, useState } from "react";

interface ChatMessage {
  role: "user" | "assistant";
  content: string;
}

const PLACEHOLDERS = [
  "I'm starting a wealth management business in Dallas — who should I connect with?",
  "Moving to NYC for accounting — who should I talk to?",
  "Which alumni are in private equity in Houston?",
  "Who works in investment banking I could ask for advice?",
  "I want to break into hedge funds — any Titans to reach out to?",
];

const PLACEHOLDER_INTERVAL_MS = 4200;

// Render assistant text with inline [label](/person/slug) links turned into
// clickable chips. Everything else is plain text; we keep this deliberately
// small rather than pulling in a markdown dependency.
const LINK_RE = /\[([^\]]+)\]\((\/person\/[a-z0-9-]+)\)/gi;

function renderContent(text: string): React.ReactNode[] {
  const nodes: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;
  let key = 0;
  LINK_RE.lastIndex = 0;
  while ((match = LINK_RE.exec(text)) !== null) {
    if (match.index > lastIndex) {
      nodes.push(text.slice(lastIndex, match.index));
    }
    nodes.push(
      <Link key={`lnk-${key++}`} href={match[2]} className="chat-chip">
        {match[1]}
      </Link>
    );
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    nodes.push(text.slice(lastIndex));
  }
  return nodes;
}

export default function ChatBar() {
  const [placeholderIdx, setPlaceholderIdx] = useState(0);
  const [input, setInput] = useState("");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (streaming || messages.length > 0) return;
    const id = setInterval(() => {
      setPlaceholderIdx((i) => (i + 1) % PLACEHOLDERS.length);
    }, PLACEHOLDER_INTERVAL_MS);
    return () => clearInterval(id);
  }, [streaming, messages.length]);

  useEffect(() => {
    panelRef.current?.scrollTo({ top: panelRef.current.scrollHeight });
  }, [messages]);

  async function send() {
    const question = input.trim();
    if (!question || streaming) return;

    const nextHistory: ChatMessage[] = [
      ...messages,
      { role: "user", content: question },
    ];
    setMessages([...nextHistory, { role: "assistant", content: "" }]);
    setInput("");
    setError(null);
    setStreaming(true);

    try {
      const res = await fetch("/api/chat", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ messages: nextHistory }),
      });

      if (!res.body) {
        throw new Error("No response stream.");
      }

      const rejected = res.headers.get("x-chat-status") === "rejected";
      const reader = res.body.getReader();
      const decoder = new TextDecoder();
      let acc = "";

      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        acc += decoder.decode(value, { stream: true });
        setMessages((prev) => {
          const copy = prev.slice();
          copy[copy.length - 1] = { role: "assistant", content: acc };
          return copy;
        });
      }

      if (rejected && !acc.trim()) {
        setError("That request couldn't be handled. Try rephrasing.");
      }
    } catch {
      setError("Something went wrong. Please try again.");
      setMessages((prev) => prev.slice(0, -1));
    } finally {
      setStreaming(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  const open = messages.length > 0 || streaming;

  return (
    <div className="chat-bar-wrap">
      <div className="chat-bar">
        <textarea
          className="chat-input"
          rows={1}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={PLACEHOLDERS[placeholderIdx]}
          maxLength={500}
          disabled={streaming}
          aria-label="Ask about Titans of Investing alumni"
        />
        <button
          className="chat-send"
          onClick={send}
          disabled={streaming || input.trim().length === 0}
          aria-label="Ask"
        >
          {streaming ? "…" : "Ask"}
        </button>
      </div>

      {open && (
        <div className="chat-panel" ref={panelRef} aria-live="polite">

          {messages.map((m, i) => (
            <div key={i} className={`chat-msg ${m.role}`}>
              {m.role === "assistant" && m.content === "" && streaming ? (
                <span className="chat-typing">Thinking…</span>
              ) : (
                renderContent(m.content)
              )}
            </div>
          ))}
          {error && <div className="chat-error">{error}</div>}
        </div>
      )}
    </div>
  );
}
