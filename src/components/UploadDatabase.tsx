import { useRef, useState } from "react";
import { Database } from "lucide-react";

import { attachMySQLDatabase, listMySQLDatabases, uploadDatabase } from "../services/api";
import type { DatabaseInfo } from "../types/database";

interface UploadDatabaseProps {
  onUploadSuccess: (database: DatabaseInfo) => void;
}

export default function UploadDatabase({
  onUploadSuccess,
}: UploadDatabaseProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  const [uploading, setUploading] = useState(false);
  const [open, setOpen] = useState(false);
  const [mode, setMode] = useState<"upload" | "connect">("upload");
  const [uploadingFileType, setUploadingFileType] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [databases, setDatabases] = useState<string[]>([]);
  const [selectedDatabase, setSelectedDatabase] = useState("");
  const [loadingDatabases, setLoadingDatabases] = useState(false);

  const openFilePicker = () => {
    setError(null);
    inputRef.current?.click();
  };

  const handleUpload = async (
    e: React.ChangeEvent<HTMLInputElement>
  ) => {
    const file = e.target.files?.[0];

    if (!file) return;

    setUploading(true);
    setUploadingFileType(file.name.toLowerCase().endsWith(".sql") ? "sql" : "database");
    setError(null);

    try {
      const result = await uploadDatabase(file);

      if (result.success && result.database) {
        onUploadSuccess(result.database);
        setOpen(false);
      } else {
        setError(result.detail ?? "Unable to upload database.");
      }
    } catch (err) {
      setError(
        err instanceof Error
          ? err.message
          : "Unable to upload database."
      );
      console.error(err);
    } finally {
      setUploading(false);

      if (inputRef.current) {
        inputRef.current.value = "";
      }
      setUploadingFileType(null);
    }
  };

  const loadDatabases = async () => {
    setLoadingDatabases(true);
    setError(null);
    try {
      const result = await listMySQLDatabases();
      setDatabases(result.databases);
      setSelectedDatabase(result.databases[0] ?? "");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to list MySQL databases.");
    } finally {
      setLoadingDatabases(false);
    }
  };

  const connectDatabase = async () => {
    if (!selectedDatabase) return;
    setUploading(true);
    setError(null);
    try {
      const result = await attachMySQLDatabase(selectedDatabase);
      if (result.success && result.database) {
        onUploadSuccess(result.database);
        setOpen(false);
      } else {
        setError(result.detail ?? "Unable to connect database.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to connect database.");
    } finally {
      setUploading(false);
    }
  };

  return (
    <>
      <div className="relative">
        <button
          type="button"
          onClick={() => {
            setOpen((value) => !value);
            if (!open && mode === "connect") void loadDatabases();
          }}
          disabled={uploading}
          aria-label="Choose database source"
          title="Choose database source"
          className="
            h-11
            w-11
            rounded-lg
            border
            border-gray-300
            bg-white
            hover:bg-gray-100
            disabled:cursor-not-allowed
            disabled:opacity-60
            transition-colors
            flex
            items-center
            justify-center
            text-gray-700
          "
        >
          <Database size={20} />
        </button>

        {open && (
          <div
            className="
              absolute
              bottom-13
              left-0
              z-20
              w-64
              rounded-lg
              border
              border-gray-200
              bg-white
              px-2
              py-2
              text-xs
              text-gray-700
              shadow-lg
            "
          >
            <div className="grid grid-cols-2 gap-1 rounded-lg bg-gray-100 p-1">
              <button
                type="button"
                aria-label="Upload SQL dump"
                onClick={() => {
                  setMode("upload");
                  openFilePicker();
                }}
                disabled={uploading}
                className={`rounded-md px-2 py-1.5 text-xs font-medium ${mode === "upload" ? "bg-white shadow-sm" : ""}`}
              >
                Upload SQL Dump
              </button>
              <button
                type="button"
                aria-label="Connect existing database"
                onClick={() => {
                  setMode("connect");
                  void loadDatabases();
                }}
                disabled={uploading}
                className={`rounded-md px-2 py-1.5 text-xs font-medium ${mode === "connect" ? "bg-white shadow-sm" : ""}`}
              >
                Connect Existing
              </button>
            </div>

            {mode === "upload" ? (
              <p className="mt-2 px-1 text-[11px] leading-4 text-gray-500">
                Supports .sql, .db, .sqlite, and .sqlite3 files.
              </p>
            ) : (
              <div className="mt-2 space-y-2">
                {loadingDatabases && (
                  <div className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm text-gray-600">
                    Loading databases...
                  </div>
                )}

                {!loadingDatabases && databases.length === 0 && !error && (
                  <div className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 leading-5 text-amber-800">
                    No MySQL databases found.
                  </div>
                )}

                {databases.length > 0 && (
                <select
                  value={selectedDatabase}
                  onChange={(event) => setSelectedDatabase(event.target.value)}
                  disabled={loadingDatabases || uploading}
                  className="w-full rounded-lg border border-gray-300 px-2 py-2 text-sm"
                >
                  {databases.map((database) => (
                    <option key={database} value={database}>
                      {database}
                    </option>
                  ))}
                </select>
                )}

                <div className="grid grid-cols-2 gap-2">
                  <button
                    type="button"
                    onClick={loadDatabases}
                    disabled={loadingDatabases || uploading}
                    className="rounded-lg border border-gray-300 px-3 py-2 text-sm font-medium text-gray-800 disabled:opacity-60"
                  >
                    Refresh
                  </button>
                <button
                  type="button"
                  onClick={connectDatabase}
                  disabled={!selectedDatabase || uploading || loadingDatabases}
                    className="rounded-lg bg-gray-950 px-3 py-2 text-sm font-medium text-white disabled:opacity-60"
                >
                    Connect
                </button>
                </div>
              </div>
            )}

            {error && (
              <div className="mt-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 leading-5 text-red-700">
                {error}
              </div>
            )}
          </div>
        )}
      </div>

      <input
        ref={inputRef}
        type="file"
        accept=".db,.sqlite,.sqlite3,.sql"
        onChange={handleUpload}
        className="sr-only"
        tabIndex={-1}
      />

      {uploading && (
        <div
          className="
            fixed
            inset-0
            bg-black/30
            flex
            items-center
            justify-center
            z-[999]
          "
        >
          <div
            className="
              bg-white
              rounded-lg
              p-6
              shadow-xl
              text-center
            "
          >
            <div
              className="
                h-10
                w-10
                rounded-full
                border-[3px]
                border-gray-950
                border-t-transparent
                animate-spin
                mx-auto
              "
            />

            <p className="mt-4 text-sm text-gray-700">
              {uploadingFileType === "sql"
                ? "Preparing database -> Importing..."
                : "Uploading database..."}
            </p>
          </div>
        </div>
      )}
    </>
  );
}
