import { useEffect, useRef } from "react";
import type { Message } from "../types/chat";
import {
  buildResponsePresentation,
  type CellValue,
  type DisplayTable,
  formatCell,
  isNumericColumn,
  labelColumn,
} from "../utils/responsePresentation";

interface ChatMessagesProps {
  messages: Message[];
  loading: boolean;
  onSend?: (message: string) => void;
}

export default function ChatMessages({
  messages,
  loading,
  onSend,
}: ChatMessagesProps) {
  const bottomRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({
      behavior: "smooth",
    });
  }, [messages, loading]);

  function renderAssistantMessage(message: Message) {
    const presentation = buildResponsePresentation(message.response);
    const confidence = message.response?.confidence;

    if (!presentation) {
      const clarificationOptions = extractClarificationOptions(message.content);
      return (
        <div className="space-y-3">
          <div className="whitespace-pre-wrap">{message.content}</div>
          {onSend && clarificationOptions.length > 0 && (
            <div className="flex flex-wrap gap-2">
              {clarificationOptions.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  onClick={() => onSend(option.value)}
                  className="rounded-full border border-gray-200 bg-gray-50 px-3 py-1.5 text-xs font-medium text-gray-700 hover:border-gray-300 hover:bg-white"
                >
                  {option.label}
                </button>
              ))}
            </div>
          )}
          {confidence && renderConfidence(confidence)}
        </div>
      );
    }

    return (
      <div className="space-y-3">
        <p>{presentation.summary}</p>

        {presentation.table && (
          <div className="rounded-md border border-gray-200 bg-white">
            <div className="flex items-center justify-end gap-2 border-b border-gray-100 px-2 py-1.5">
              <button
                type="button"
                onClick={() => copyTable(presentation.table)}
                className="rounded-md border border-gray-200 px-2 py-1 text-[11px] font-medium text-gray-600 hover:bg-gray-50"
              >
                Copy Table
              </button>
              {onSend && (
                <button
                  type="button"
                  onClick={() => onSend("Explain this result in simple words.")}
                  className="rounded-md border border-gray-200 px-2 py-1 text-[11px] font-medium text-gray-600 hover:bg-gray-50"
                >
                  Explain
                </button>
              )}
            </div>

            <div className="max-h-80 overflow-auto">
              <table className="min-w-full border-separate border-spacing-0 text-left text-[13px]">
              <thead className="sticky top-0 z-10 bg-gray-50 text-gray-500">
                <tr>
                  {presentation.table.columns.map((column, index) => (
                    <th
                      key={column}
                      className={`border-b border-gray-200 px-3 py-2 font-semibold ${
                        isNumericColumn(column) ? "text-right" : "text-left"
                      } ${
                        index === 1
                          ? "sticky left-0 z-20 bg-gray-50 shadow-[1px_0_0_#e5e7eb]"
                          : ""
                      }`}
                    >
                      {labelColumn(column)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {presentation.table.rows.map((row, rowIndex) => (
                  <tr key={rowIndex} className="odd:bg-white even:bg-gray-50/70">
                    {presentation.table?.columns.map((column, columnIndex) => (
                      <td
                        key={column}
                        className={`border-b border-gray-100 px-3 py-2 text-gray-800 ${
                          isNumericColumn(column) ? "text-right font-medium tabular-nums" : "text-left"
                        } ${
                          columnIndex === 1
                            ? "sticky left-0 z-10 max-w-52 bg-inherit font-medium shadow-[1px_0_0_#e5e7eb]"
                            : ""
                        }`}
                      >
                        {formatCell(column, row[column] as CellValue)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </div>
        )}

        {presentation.bullets.length > 0 && (
          <ul className="list-disc space-y-1 pl-5">
            {presentation.bullets.slice(0, 8).map((bullet) => (
              <li key={bullet}>{bullet}</li>
            ))}
          </ul>
        )}

        {presentation.limitations.length > 0 && (
          <details className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
            <summary className="cursor-pointer text-xs font-semibold text-gray-600">
              Limitations
            </summary>
            <div className="mt-2 space-y-1 text-xs text-gray-600">
              {presentation.limitations.map((limitation) => (
                <p key={limitation}>{limitation}</p>
              ))}
            </div>
          </details>
        )}

        {message.response?.sql && (
          <details className="rounded-md border border-gray-200 bg-gray-50 px-3 py-2">
            <summary className="cursor-pointer text-xs font-semibold text-gray-600">
              SQL details
            </summary>
            <pre className="mt-2 overflow-x-auto text-xs leading-5 text-gray-700">
              {message.response.sql}
            </pre>
          </details>
        )}

        {confidence && renderConfidence(confidence)}
      </div>
    );
  }

  function copyTable(table: DisplayTable | null) {
    if (!table) return;
    const text = [
      table.columns.map(labelColumn).join("\t"),
      ...table.rows.map((row) =>
        table.columns
          .map((column) => formatCell(column, row[column] as CellValue))
          .join("\t")
      ),
    ].join("\n");

    void navigator.clipboard?.writeText(text);
  }

  function extractClarificationOptions(content: string): Array<{ label: string; value: string }> {
    const seen = new Set<string>();
    const options: Array<{ label: string; value: string }> = [];

    for (const rawLine of content.split("\n")) {
      const line = rawLine.trim();
      let label: string | null = null;
      let value: string | null = null;

      const letterMatch = line.match(/^\*\*([A-Z])\.\*\*\s+(.+)$/);
      if (letterMatch) {
        value = letterMatch[1];
        label = `${letterMatch[1]}. ${letterMatch[2].replace(/\*\*/g, "").trim()}`;
      }

      const boldLetterMatch = line.match(/^\*\*([A-Z])\.\s+(.+?)\*\*$/);
      if (!label && boldLetterMatch) {
        value = boldLetterMatch[1];
        label = `${boldLetterMatch[1]}. ${boldLetterMatch[2].replace(/\*\*/g, "").trim()}`;
      }

      const numericMatch = line.match(/^(\d+)\.\s+(.+)$/);
      if (!label && numericMatch) {
        value = numericMatch[2].trim();
        label = `${numericMatch[1]}. ${numericMatch[2].trim()}`;
      }

      if (!label || !value || label.length > 100 || label.includes("`")) continue;
      if (seen.has(value)) continue;
      seen.add(value);
      options.push({ label, value });
    }

    return options.slice(0, 8);
  }

  function renderConfidence(confidence: NonNullable<Message["response"]>["confidence"]) {
    if (!confidence) return null;
    const levelStyles = {
      high: "text-emerald-700",
      medium: "text-amber-700",
      low: "text-red-700",
    }[confidence.confidence_level];

    return (
      <div className="border-t border-gray-100 pt-2 text-[11px] text-gray-500">
        <div className="flex items-center justify-between gap-3">
          <span className={`font-semibold capitalize ${levelStyles}`}>
            Confidence: {confidence.confidence_level} ({confidence.confidence_score}%)
          </span>
        </div>
        <details className="mt-1">
          <summary className="cursor-pointer font-medium">Why?</summary>
          <ul className="mt-2 list-disc space-y-1 pl-4">
            {confidence.confidence_reasons.slice(0, 4).map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
          {confidence.limitations.length > 0 && (
            <div className="mt-2">
              <div className="font-medium">Limitations</div>
              <ul className="mt-1 list-disc space-y-1 pl-4">
                {confidence.limitations.slice(0, 3).map((limitation) => (
                  <li key={limitation}>{limitation}</li>
                ))}
              </ul>
            </div>
          )}
        </details>
      </div>
    );
  }

  return (
    <div
      className="
        flex-1
        overflow-y-auto
        bg-gray-100
        px-4
        py-4
        space-y-3
      "
    >
      {messages.map((message, index) => (
        <div key={index}>
          <div
            className={`flex ${
              message.role === "user"
                ? "justify-end"
                : "justify-start"
            }`}
          >
            <div
              className={`max-w-[88%] rounded-lg border px-3.5 py-2.5 text-sm leading-6 ${
                message.role === "user"
                  ? "border-gray-950 bg-gray-950 text-white"
                  : "border-gray-200 bg-white text-gray-800"
              }`}
            >
              {message.role === "assistant"
                ? renderAssistantMessage(message)
                : message.content}
            </div>
          </div>
        </div>
      ))}

      {loading && (
        <div className="flex justify-start">
          <div
            className="
              bg-white
              rounded-lg
              border
              border-gray-200
              px-3
              py-2
              flex
              items-center
              gap-2
              text-gray-500
            "
          >
            <span className="text-xs font-medium">Checking verified ERP data</span>
            <span className="animate-bounce">●</span>
            <span
              className="animate-bounce"
              style={{ animationDelay: "0.2s" }}
            >
              ●
            </span>
            <span
              className="animate-bounce"
              style={{ animationDelay: "0.4s" }}
            >
              ●
            </span>
          </div>
        </div>
      )}

      <div ref={bottomRef} />
    </div>
  );
}
