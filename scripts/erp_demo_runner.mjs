const EDGE_PATH = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const FRONTEND_URL = "http://127.0.0.1:5173";
const API_URL = "http://127.0.0.1:8000";
const DATABASE_NAME = "rcerp_jul8_manual_20260714_094608";
const SESSION_ID = "obs-demo-session";
const SMOKE_MODE = process.argv.includes("--smoke");
const PREPARE_ONLY = process.argv.includes("--prepare-only");
const SKIP_OPEN = process.argv.includes("--skip-open");

const questions = [
  "What projects are currently running?",
  "Show me the details of RDSS-VIZAG.",
  "Show me the top five projects by project value.",
  "What's our total billing?",
  "Compare the billing of RDSS-VIZAG and RDSS-UGCABLE.",
  "Show me the billing for this year.",
  "What's the total purchase order value?",
  "Show me the top five purchase orders by value.",
  "Show me purchase order value by project.",
  "Which vendors have the highest purchase order value?",
  "Show vendor-wise purchase order value.",
  "Show material issued during the last 30 days.",
  "Show top ten materials issued.",
  "Compare RDSS-VIZAG and RDSS-UGCABLE by total billing and total purchase order value.",
];

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const jitter = (min, max) => Math.floor(min + Math.random() * (max - min));

async function attachDatabase() {
  await fetch(`${API_URL}/clear`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      session_id: SESSION_ID,
    }),
  }).catch(() => {});
  const response = await fetch(`${API_URL}/mysql/attach`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      database: DATABASE_NAME,
      session_id: SESSION_ID,
    }),
  });
  if (!response.ok) {
    throw new Error(`Database attach failed: ${response.status} ${await response.text()}`);
  }
}

async function openEdge() {
  const { spawn } = await import("node:child_process");
  spawn(EDGE_PATH, [
    "--remote-debugging-port=9222",
    "--new-window",
    "--start-maximized",
    "--user-data-dir=C:\\Users\\moksh\\AppData\\Local\\Temp\\erp-ai-demo-edge",
    FRONTEND_URL,
  ], {
    detached: true,
    stdio: "ignore",
  }).unref();
  await sleep(3500);
}

async function getWebSocketUrl() {
  for (let attempt = 0; attempt < 30; attempt += 1) {
    try {
      const targets = await fetch("http://127.0.0.1:9222/json").then((r) => r.json());
      const page = targets.find((target) => target.type === "page" && target.url.includes("127.0.0.1:5173"))
        ?? targets.find((target) => target.type === "page");
      if (page?.webSocketDebuggerUrl) return page.webSocketDebuggerUrl;
    } catch {
      // Edge may still be starting.
    }
    await sleep(500);
  }
  throw new Error("Could not connect to Edge remote debugging.");
}

function createCdpClient(wsUrl) {
  const ws = new WebSocket(wsUrl);
  let nextId = 1;
  const pending = new Map();

  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    }
  });

  return new Promise((resolve, reject) => {
    ws.addEventListener("open", () => {
      resolve({
        send(method, params = {}) {
          const id = nextId;
          nextId += 1;
          ws.send(JSON.stringify({ id, method, params }));
          return new Promise((resolveCommand, rejectCommand) => {
            pending.set(id, { resolve: resolveCommand, reject: rejectCommand });
          });
        },
        close() {
          ws.close();
        },
      });
    });
    ws.addEventListener("error", reject);
  });
}

async function evaluate(client, expression) {
  return client.send("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
  });
}

async function waitFor(client, expression, timeoutMs = 120000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    const result = await evaluate(client, expression);
    if (result.result?.value) return;
    await sleep(500);
  }
  throw new Error(`Timed out waiting for: ${expression}`);
}

async function typeHuman(client, text) {
  for (const char of text) {
    await client.send("Input.insertText", { text: char });
    await sleep(jitter(45, 120));
    if (/[,.?]/.test(char)) await sleep(jitter(120, 280));
    if (char === " " && Math.random() < 0.08) await sleep(jitter(120, 260));
  }
}

async function askQuestion(client, question) {
  await evaluate(client, `
    (() => {
      const input = document.querySelector('input[placeholder^="Ask"]');
      input.focus();
      input.value = '';
      input.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    })()
  `);
  await typeHuman(client, question);
  await sleep(1000);
  await client.send("Input.dispatchKeyEvent", { type: "keyDown", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await client.send("Input.dispatchKeyEvent", { type: "keyUp", key: "Enter", code: "Enter", windowsVirtualKeyCode: 13 });
  await sleep(1200);
  await waitFor(client, `!document.body.innerText.includes('Checking verified ERP data')`, 120000);
  await sleep(5000);
  await evaluate(client, `
    (async () => {
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const tables = Array.from(document.querySelectorAll('table'));
      const table = tables[tables.length - 1];
      const scroller = table?.closest('[class*="overflow-x"], .overflow-x-auto, .overflow-auto')
        || table?.parentElement;
      if (scroller && scroller.scrollWidth > scroller.clientWidth) {
        scroller.scrollTo({ left: Math.round(scroller.scrollWidth * 0.45), behavior: 'smooth' });
        await sleep(1800);
        scroller.scrollTo({ left: scroller.scrollWidth, behavior: 'smooth' });
        await sleep(1800);
        scroller.scrollTo({ left: 0, behavior: 'smooth' });
        await sleep(1200);
      }
      return true;
    })()
  `);
  await evaluate(client, `
    (() => {
      const scroller = document.querySelector('.overflow-y-auto');
      if (scroller) scroller.scrollBy({ top: 220, behavior: 'smooth' });
      return true;
    })()
  `);
  await sleep(jitter(800, 1400));
}

async function chooseClarificationOption(client, optionPattern) {
  await evaluate(client, `
    (() => {
      const pattern = new RegExp(${JSON.stringify(optionPattern)}, 'i');
      const buttons = Array.from(document.querySelectorAll('button'));
      const option = buttons.find((button) => pattern.test(button.textContent || ''));
      if (option) option.click();
      return Boolean(option);
    })()
  `);
  await sleep(1200);
  await waitFor(client, `!document.body.innerText.includes('Checking verified ERP data')`, 120000);
  await sleep(5000);
  await evaluate(client, `
    (async () => {
      const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
      const tables = Array.from(document.querySelectorAll('table'));
      const table = tables[tables.length - 1];
      const scroller = table?.closest('[class*="overflow-x"], .overflow-x-auto, .overflow-auto')
        || table?.parentElement;
      if (scroller && scroller.scrollWidth > scroller.clientWidth) {
        scroller.scrollTo({ left: scroller.scrollWidth, behavior: 'smooth' });
        await sleep(1800);
        scroller.scrollTo({ left: 0, behavior: 'smooth' });
      }
      return true;
    })()
  `);
}

async function runClarification(client) {
  await askQuestion(client, "Show top vendors.");
  await askQuestion(client, "PO total value");

  await askQuestion(client, "Show project details.");
  await askQuestion(client, "RDSS-VIZAG");

  await askQuestion(client, "Compare purchase orders.");
  await askQuestion(client, "RDSS-VIZAG and RDSS-UGCABLE");
  await askQuestion(client, "PO total value");

  await askQuestion(client, "Compare vendors.");
  await askQuestion(client, "POLYCAB INDIA LIMITED and KEI Industries");
  await askQuestion(client, "PO total value");
}

async function main() {
  console.log("Demo runner: attaching database");
  await attachDatabase();
  if (!SKIP_OPEN) {
    console.log("Demo runner: opening Edge");
    await openEdge();
  }
  console.log("Demo runner: connecting to Edge CDP");
  const client = await createCdpClient(await getWebSocketUrl());
  await client.send("Runtime.enable");
  await client.send("Page.enable");
  console.log("Demo runner: preparing page");
  await evaluate(client, `localStorage.setItem('sql-assistant-session-id', '${SESSION_ID}')`);
  await evaluate(client, `location.href = '${FRONTEND_URL}'`);
  await sleep(2500);
  await evaluate(client, `document.body.style.zoom = '1'`);
  await sleep(PREPARE_ONLY || SMOKE_MODE ? 2500 : 15000);
  await evaluate(client, `document.querySelector('button[aria-label="Open chat"]')?.click()`);
  await sleep(2500);
  if (SMOKE_MODE || PREPARE_ONLY) {
    console.log("Demo runner: waiting for chat input");
    await waitFor(client, `Boolean(document.querySelector('input[placeholder^="Ask"]'))`, 20000);
    console.log("Demo runner: ready");
    client.close();
    return;
  }

  for (const question of questions) {
    await askQuestion(client, question);
  }
  await runClarification(client);
  await sleep(3000);
  client.close();
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
