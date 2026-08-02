import type { ChatResponse } from "../types/chat";
import type { UploadResponse } from "../types/database";
import { getSessionId } from "./session";

const API = import.meta.env.VITE_API_URL ?? "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 12_000;
const CHAT_TIMEOUT_MS = 90_000;
const UPLOAD_TIMEOUT_MS = 15 * 60_000;

type ApiErrorResponse = {
    success?: false;
    detail?: string;
    message?: string;
};

async function readJsonResponse<T>(response: Response): Promise<T> {
    const data = await response.json().catch(() => null);

    if (!response.ok) {
        const message =
            data?.detail ??
            data?.message ??
            `Request failed with status ${response.status}`;

        throw new Error(message);
    }

    return data as T;
}

async function fetchWithTimeout(
    input: RequestInfo | URL,
    init?: RequestInit,
    timeoutMs = REQUEST_TIMEOUT_MS
) {
    const controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

    try {
        return await fetch(input, {
            ...init,
            signal: controller.signal
        });
    } catch (error) {
        if (error instanceof DOMException && error.name === "AbortError") {
            throw new Error("Request timed out. Check that the backend and Docker database are running.");
        }
        throw error;
    } finally {
        window.clearTimeout(timeout);
    }
}

export async function uploadDatabase(file: File): Promise<UploadResponse & ApiErrorResponse> {

    const formData = new FormData();

    formData.append("file", file);
    formData.append("session_id", getSessionId());

    const response = await fetchWithTimeout(`${API}/upload`, {

        method: "POST",

        body: formData

    }, UPLOAD_TIMEOUT_MS);

    return readJsonResponse<UploadResponse & ApiErrorResponse>(response);
}

export async function listMySQLDatabases(): Promise<{ success: boolean; databases: string[] }> {
    const response = await fetchWithTimeout(`${API}/mysql/databases`);

    return readJsonResponse<{ success: boolean; databases: string[] }>(response);
}

export async function attachMySQLDatabase(database: string): Promise<UploadResponse & ApiErrorResponse> {
    const response = await fetchWithTimeout(`${API}/mysql/attach`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            database,
            session_id: getSessionId()
        })
    }, 30_000);

    return readJsonResponse<UploadResponse & ApiErrorResponse>(response);
}

export async function sendMessage(question: string): Promise<ChatResponse> {

    const response = await fetchWithTimeout(`${API}/chat`, {

        method: "POST",

        headers: {
            "Content-Type": "application/json"
        },

        body: JSON.stringify({
            question,
            session_id: getSessionId()
        })

    }, CHAT_TIMEOUT_MS);

    return readJsonResponse<ChatResponse>(response);
}

export async function getStatus() {

    const response = await fetchWithTimeout(
        `${API}/status?session_id=${encodeURIComponent(getSessionId())}`
    );

    return readJsonResponse(response);
}

export async function clearSession() {

    const response = await fetchWithTimeout(`${API}/clear`, {
        method: "POST",
        headers: {
            "Content-Type": "application/json"
        },
        body: JSON.stringify({
            session_id: getSessionId()
        })
    });

    return readJsonResponse(response);
}
