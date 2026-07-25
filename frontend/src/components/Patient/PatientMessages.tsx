import { useState, useEffect, useCallback } from "react";
import { AlertCircle, MessageSquare, Send, ArrowLeft, Plus } from "lucide-react";
import type { User } from "@/hooks/useAuth";
import api from "@/api/axios";

interface Message {
  id: number;
  patient_user: number;
  parent: number | null;
  sender: number | null;
  sender_name: string;
  subject: string;
  message: string;
  sender_is_patient: boolean;
  is_read: boolean;
  read_at: string | null;
  reply_count: number;
  created_at: string;
}

function formatDate(dateStr: string): string {
  try {
    const d = new Date(dateStr);
    const now = new Date();
    const diffMs = now.getTime() - d.getTime();
    const diffDays = Math.floor(diffMs / (1000 * 60 * 60 * 24));

    if (diffDays === 0) {
      return d.toLocaleTimeString(undefined, {
        hour: "numeric",
        minute: "2-digit",
      });
    }
    if (diffDays === 1) return "Yesterday";
    if (diffDays < 7) {
      return d.toLocaleDateString(undefined, { weekday: "short" });
    }
    return d.toLocaleDateString(undefined, {
      year: "numeric",
      month: "short",
      day: "numeric",
    });
  } catch {
    return dateStr;
  }
}

type View = "list" | "conversation" | "compose";

export default function PatientMessages({ user }: { user: User | null }) {
  const [threads, setThreads] = useState<Message[]>([]);
  const [activeThread, setActiveThread] = useState<Message | null>(null);
  const [replies, setReplies] = useState<Message[]>([]);
  const [view, setView] = useState<View>("list");
  const [loading, setLoading] = useState(true);
  const [loadingReplies, setLoadingReplies] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Compose form state
  const [composeSubject, setComposeSubject] = useState("");
  const [composeMessage, setComposeMessage] = useState("");

  // Reply state
  const [replyMessage, setReplyMessage] = useState("");

  const fetchThreads = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.get("/v1/messages/?parent=null");
      const data = Array.isArray(res.data) ? res.data : res.data.results ?? [];
      setThreads(data);
    } catch {
      setError("Failed to load messages. Please try again.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (user && user.person_id != null) {
      fetchThreads();
    } else {
      setLoading(false);
    }
  }, [user, fetchThreads]);

  const openThread = async (thread: Message) => {
    setActiveThread(thread);
    setView("conversation");
    setReplyMessage("");
    setError(null);

    // Mark as read if it's from a provider and unread
    if (!thread.sender_is_patient && !thread.read_at) {
      try {
        await api.patch(`/v1/messages/${thread.id}/mark-read/`);
        setThreads((prev) =>
          prev.map((t) =>
            t.id === thread.id
              ? { ...t, is_read: true, read_at: new Date().toISOString() }
              : t
          )
        );
        thread = { ...thread, is_read: true, read_at: new Date().toISOString() };
        setActiveThread(thread);
      } catch {
        // Non-critical — continue showing conversation
      }
    }

    // Fetch replies
    setLoadingReplies(true);
    try {
      const res = await api.get(`/v1/messages/?parent=${thread.id}`);
      const data = Array.isArray(res.data) ? res.data : res.data.results ?? [];
      setReplies(data);
    } catch {
      setError("Failed to load replies. Please try again.");
    } finally {
      setLoadingReplies(false);
    }
  };

  const handleSendReply = async () => {
    if (!replyMessage.trim() || !activeThread) return;
    setSending(true);
    setError(null);
    try {
      const res = await api.post("/v1/messages/", {
        subject: `Re: ${activeThread.subject}`,
        message: replyMessage.trim(),
        parent: activeThread.id,
      });
      setReplies((prev) => [...prev, res.data]);
      setReplyMessage("");
      // Update reply count on thread
      setThreads((prev) =>
        prev.map((t) =>
          t.id === activeThread.id
            ? { ...t, reply_count: t.reply_count + 1 }
            : t
        )
      );
    } catch {
      setError("Failed to send reply. Please try again.");
    } finally {
      setSending(false);
    }
  };

  const handleCompose = async () => {
    if (!composeSubject.trim() || !composeMessage.trim()) return;
    setSending(true);
    setError(null);
    try {
      await api.post("/v1/messages/", {
        subject: composeSubject.trim(),
        message: composeMessage.trim(),
      });
      setComposeSubject("");
      setComposeMessage("");
      setView("list");
      await fetchThreads();
    } catch {
      setError("Failed to send message. Please try again.");
    } finally {
      setSending(false);
    }
  };

  const handleBack = () => {
    setView("list");
    setActiveThread(null);
    setReplies([]);
    setError(null);
  };

  // --- No record state ---
  if (!user || user.person_id == null) {
    return (
      <div className="rounded-2xl bg-background p-8 text-center shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <AlertCircle className="mx-auto mb-3 h-10 w-10 text-amber-400" />
        <p className="text-sm text-portal-text-secondary">
          No health record is linked to your account. Messaging will be
          available once your record is set up.
        </p>
      </div>
    );
  }

  // --- Loading state ---
  if (loading) {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <div className="flex items-center gap-3 mb-6">
          <div className="h-5 w-5 animate-pulse rounded bg-muted" />
          <div className="h-5 w-32 animate-pulse rounded bg-muted" />
        </div>
        <div className="space-y-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="flex items-center justify-between rounded-lg border border-border p-4"
            >
              <div className="space-y-1.5">
                <div className="h-4 w-48 animate-pulse rounded bg-muted" />
                <div className="h-3 w-32 animate-pulse rounded bg-muted" />
              </div>
              <div className="h-3 w-16 animate-pulse rounded bg-muted" />
            </div>
          ))}
        </div>
      </div>
    );
  }

  // --- Compose view ---
  if (view === "compose") {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <button
          onClick={handleBack}
          className="mb-4 inline-flex items-center gap-1 text-sm font-medium text-portal-brand hover:underline"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to messages
        </button>

        <div className="flex items-center gap-2 mb-6">
          <MessageSquare className="h-5 w-5 text-portal-brand" />
          <h2 className="text-xl font-bold text-foreground">New Message</h2>
        </div>

        {error && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label
              htmlFor="compose-subject"
              className="mb-1 block text-sm font-medium text-foreground"
            >
              Subject
            </label>
            <input
              id="compose-subject"
              type="text"
              value={composeSubject}
              onChange={(e) => setComposeSubject(e.target.value)}
              placeholder="Enter subject..."
              className="w-full rounded-lg border border-border bg-background px-4 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
          <div>
            <label
              htmlFor="compose-message"
              className="mb-1 block text-sm font-medium text-foreground"
            >
              Message
            </label>
            <textarea
              id="compose-message"
              value={composeMessage}
              onChange={(e) => setComposeMessage(e.target.value)}
              placeholder="Type your message..."
              rows={5}
              className="w-full rounded-lg border border-border bg-background px-4 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary resize-none"
            />
          </div>
          <div className="flex items-center gap-3">
            <button
              onClick={handleCompose}
              disabled={
                sending || !composeSubject.trim() || !composeMessage.trim()
              }
              className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
            >
              <Send className="h-4 w-4" />
              {sending ? "Sending..." : "Send"}
            </button>
            <button
              onClick={handleBack}
              disabled={sending}
              className="rounded-md border border-border px-4 py-2 text-sm font-medium text-foreground hover:bg-muted/50 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Cancel
            </button>
          </div>
        </div>
      </div>
    );
  }

  // --- Conversation view ---
  if (view === "conversation" && activeThread) {
    return (
      <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
        <button
          onClick={handleBack}
          className="mb-4 inline-flex items-center gap-1 text-sm font-medium text-portal-brand hover:underline"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to messages
        </button>

        <h2 className="mb-6 text-xl font-bold text-foreground">
          {activeThread.subject}
        </h2>

        {error && (
          <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
            <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
            <p className="text-sm text-red-700">{error}</p>
          </div>
        )}

        {/* Messages */}
        <div className="mb-6 space-y-4">
          {/* Original message */}
          <MessageBubble msg={activeThread} />

          {loadingReplies ? (
            <div className="space-y-3">
              {[1, 2].map((i) => (
                <div
                  key={i}
                  className="h-16 animate-pulse rounded-lg bg-muted"
                />
              ))}
            </div>
          ) : (
            replies.map((reply) => (
              <MessageBubble key={reply.id} msg={reply} />
            ))
          )}
        </div>

        {/* Reply input */}
        <div className="border-t border-border pt-4">
          <div className="flex gap-3">
            <textarea
              value={replyMessage}
              onChange={(e) => setReplyMessage(e.target.value)}
              placeholder="Type your reply..."
              rows={3}
              aria-label="Reply message"
              className="flex-1 rounded-lg border border-border bg-background px-4 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary resize-none"
            />
            <button
              onClick={handleSendReply}
              disabled={sending || !replyMessage.trim()}
              className="self-end rounded-md bg-primary p-2.5 text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50"
              aria-label="Send reply"
            >
              <Send className="h-4 w-4" />
            </button>
          </div>
        </div>
      </div>
    );
  }

  // --- Thread list view (default) ---
  return (
    <div className="rounded-2xl bg-background p-8 shadow-[0_1px_3px_rgba(0,0,0,0.06),0_6px_24px_rgba(0,0,0,0.06)]">
      <div className="flex items-center justify-between mb-1">
        <div className="flex items-center gap-2">
          <MessageSquare className="h-5 w-5 text-portal-brand" />
          <h2 className="text-xl font-bold text-foreground">Messages</h2>
        </div>
        <button
          onClick={() => {
            setView("compose");
            setError(null);
            setComposeSubject("");
            setComposeMessage("");
          }}
          className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground hover:bg-primary/90"
        >
          <Plus className="h-4 w-4" />
          New Message
        </button>
      </div>
      <p className="mb-6 text-sm text-muted-foreground">
        Send and receive messages with your care team.
      </p>

      {error && (
        <div className="mb-4 flex items-center gap-2 rounded-lg border border-red-200 bg-red-50 px-4 py-3">
          <AlertCircle className="h-4 w-4 shrink-0 text-red-500" />
          <p className="text-sm text-red-700">{error}</p>
        </div>
      )}

      {threads.length === 0 && !error && (
        <p className="text-sm text-muted-foreground">
          No messages yet. Start a conversation with your care team.
        </p>
      )}

      <div className="space-y-3">
        {threads.map((thread) => {
          const isUnread = !thread.read_at && !thread.sender_is_patient;
          return (
            <button
              key={thread.id}
              onClick={() => openThread(thread)}
              className="w-full text-left flex items-center justify-between rounded-lg border border-border px-5 py-4 transition-colors hover:bg-muted/30"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  {isUnread && (
                    <span className="h-2.5 w-2.5 shrink-0 rounded-full bg-primary" />
                  )}
                  <p
                    className={`text-sm truncate ${
                      isUnread
                        ? "font-bold text-foreground"
                        : "font-medium text-foreground"
                    }`}
                  >
                    {thread.subject}
                  </p>
                </div>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {thread.sender_name} &middot; {formatDate(thread.created_at)}
                </p>
              </div>
              <div className="ml-4 flex items-center gap-2 shrink-0">
                {thread.reply_count > 0 && (
                  <span className="inline-flex items-center rounded-full bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">
                    {thread.reply_count}{" "}
                    {thread.reply_count === 1 ? "reply" : "replies"}
                  </span>
                )}
              </div>
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** Single message bubble — styled by sender role. */
function MessageBubble({ msg }: { msg: Message }) {
  const isPatient = msg.sender_is_patient;

  return (
    <div className={`flex ${isPatient ? "justify-end" : "justify-start"}`}>
      <div
        className={`max-w-[80%] rounded-2xl px-4 py-3 ${
          isPatient
            ? "bg-primary text-primary-foreground"
            : "bg-muted text-foreground"
        }`}
      >
        <div
          className={`mb-1 flex items-center gap-2 text-xs ${
            isPatient ? "text-primary-foreground/70" : "text-muted-foreground"
          }`}
        >
          <span className="font-medium">{msg.sender_name}</span>
          <span>&middot;</span>
          <span>{formatDate(msg.created_at)}</span>
        </div>
        <p className="text-sm whitespace-pre-wrap">{msg.message}</p>
      </div>
    </div>
  );
}
