import { useEffect, useState } from "react";
import { MessageCircle, X } from "lucide-react";

import ChatHeader from "./ChatHeader";
import ChatMessages from "./ChatMessages";
import ChatInput from "./ChatInput";

import type { DatabaseInfo } from "../types/database";
import { useChat } from "../hooks/useChat";
import { getStatus } from "../services/api";

export default function ChatWidget() {
  const [open, setOpen] = useState(false);
  const [database, setDatabase] = useState<DatabaseInfo | null>(null);
  const { messages, loading, ask } = useChat();

  useEffect(() => {
    let cancelled = false;

    async function hydrateSessionDatabase() {
      try {
        const status = await getStatus() as {
          database_connected?: boolean;
          database?: {
            database_name?: string | null;
            source_type?: string | null;
            connection_id?: string | null;
            tables?: number | null;
            columns?: number | null;
          };
        };

        if (cancelled || !status.database_connected || !status.database?.database_name) {
          return;
        }

        setDatabase({
          name: status.database.database_name,
          type: (status.database.source_type || "database").toUpperCase(),
          tables: status.database.tables ?? 0,
          columns: status.database.columns ?? 0,
          source_type: status.database.source_type || undefined,
          connection_id: status.database.connection_id || undefined,
        });
      } catch (error) {
        console.error(error);
      }
    }

    void hydrateSessionDatabase();

    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      {/* Floating Button */}

      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-label={open ? "Close chat" : "Open chat"}
        title={open ? "Close chat" : "Open chat"}
        className="
        fixed
        bottom-6
        right-6
        h-14
        w-14
        rounded-lg
        bg-gray-950
        text-white
        shadow-lg
        flex
        items-center
        justify-center
        hover:bg-gray-800
        transition-all
        duration-150
        z-50
      "
      >
        {open ? <X size={22} /> : <MessageCircle size={22} />}
      </button>

      {/* Widget */}

      <div
        className={`
        fixed
        bottom-24
        right-6
        w-[560px]
        max-w-[calc(100vw-2rem)]
        h-[680px]
        max-h-[calc(100vh-7rem)]
        rounded-lg
        bg-white
        border
        border-gray-200
        shadow-2xl
        overflow-hidden
        transition-all
        duration-150
        origin-bottom-right
        flex
        flex-col
        z-50

        ${
          open
            ? "visible opacity-100 scale-100"
            : "invisible opacity-0 scale-95 pointer-events-none"
        }
      `}
      >
        <ChatHeader />

        <ChatMessages
          messages={messages}
          loading={loading}
          onSend={ask}
        />
            
        <ChatInput
          onSend={ask}
          database={database}
          setDatabase={setDatabase}
        />
      </div>
    </>
  );
}
