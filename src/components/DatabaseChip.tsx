import { Database, CheckCircle2, X } from "lucide-react";

interface DatabaseChipProps {
  databaseName: string;
  databaseType?: string;
  tables?: number;
  columns?: number;
  onRemove?: () => void;
  removing?: boolean;
}

export default function DatabaseChip({
  databaseName,
  databaseType = "SQLite",
  tables,
  columns,
  onRemove,
  removing = false,
}: DatabaseChipProps) {
  if (!databaseName) return null;

  return (
    <div
      className="
        mb-3
        flex
        items-center
        justify-between
        gap-3
        rounded-lg
        border
        border-gray-200
        bg-gray-50
        p-3
      "
    >
      <div className="min-w-0 flex-1 flex items-center gap-3">
        <div
          className="
            shrink-0
            flex
            h-10
            w-10
            items-center
            justify-center
            rounded-lg
            bg-gray-950
            text-white
          "
        >
          <Database size={20} />
        </div>

        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <CheckCircle2
              size={16}
              className="shrink-0 text-green-600"
            />

            <span className="truncate font-semibold text-gray-800">
              {databaseName}
            </span>
          </div>

          <p className="text-xs text-gray-500">
            {removing ? "Clearing..." : databaseType}

            {!removing &&
              tables !== undefined &&
              columns !== undefined &&
              ` • ${tables} Tables • ${columns} Columns`}
          </p>
        </div>
      </div>

      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          disabled={removing}
          aria-label="Clear selected database"
          className="
            shrink-0
            rounded-full
            p-1
            hover:bg-gray-200
            disabled:cursor-not-allowed
            disabled:opacity-50
            transition-colors
          "
          title="Clear selected database"
        >
          <X size={16} />
        </button>
      )}
    </div>
  );
}
