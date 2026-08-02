import { spawn } from "node:child_process";
import { readdir, rename, stat, writeFile } from "node:fs/promises";
import path from "node:path";

const OBS_EXE = "C:\\Program Files\\obs-studio\\bin\\64bit\\obs64.exe";
const OBS_CWD = "C:\\Program Files\\obs-studio\\bin\\64bit";
const DOWNLOADS = "C:\\Users\\moksh\\Downloads";
const FINAL_FILE = path.join(DOWNLOADS, "ERP_AI_Assistant_POC_Demo.mp4");

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function startObs() {
  const obs = spawn(OBS_EXE, [
    "--profile",
    "ERP_AI_Demo",
    "--collection",
    "ERP_AI_Demo",
    "--disable-shutdown-check",
    "--minimize-to-tray",
  ], {
    cwd: OBS_CWD,
    detached: false,
    stdio: "ignore",
  });
  return obs;
}

async function connectObsWebSocket() {
  for (let attempt = 0; attempt < 40; attempt += 1) {
    try {
      const client = await createObsClient("ws://127.0.0.1:4455");
      return client;
    } catch {
      await sleep(500);
    }
  }
  throw new Error("OBS WebSocket did not become available.");
}

function createObsClient(url) {
  const ws = new WebSocket(url);
  let requestId = 1;
  const pending = new Map();

  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.op === 0) {
      ws.send(JSON.stringify({
        op: 1,
        d: { rpcVersion: message.d.rpcVersion || 1 },
      }));
      return;
    }
    if (message.op === 7) {
      const pendingRequest = pending.get(message.d.requestId);
      if (!pendingRequest) return;
      pending.delete(message.d.requestId);
      if (message.d.requestStatus?.result) {
        pendingRequest.resolve(message.d.responseData || {});
      } else {
        pendingRequest.reject(new Error(message.d.requestStatus?.comment || "OBS request failed"));
      }
    }
  });

  return new Promise((resolve, reject) => {
    ws.addEventListener("open", () => {
      const client = {
        request(requestType, requestData = {}) {
          const id = String(requestId);
          requestId += 1;
          ws.send(JSON.stringify({
            op: 6,
            d: {
              requestType,
              requestId: id,
              requestData,
            },
          }));
          return new Promise((resolveRequest, rejectRequest) => {
            pending.set(id, {
              resolve: resolveRequest,
              reject: rejectRequest,
            });
          });
        },
        close() {
          ws.close();
        },
      };
      setTimeout(() => resolve(client), 700);
    });
    ws.addEventListener("error", reject);
  });
}

function runDemoAutomation() {
  return runDemoCommand([]);
}

function prepareDemoWindow() {
  return runDemoCommand(["--prepare-only"]);
}

function runDemoCommand(args) {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, ["scripts\\erp_demo_runner.mjs", ...args], {
      cwd: process.cwd(),
      stdio: "inherit",
    });
    child.on("exit", (code) => {
      if (code === 0) resolve();
      else reject(new Error(`Demo automation failed with exit code ${code}`));
    });
    child.on("error", reject);
  });
}

async function findEdgeWindowValue(obs, inputName) {
  const response = await obs.request("GetInputPropertiesListPropertyItems", {
    inputName,
    propertyName: "window",
  });
  const edge = response.propertyItems.find((item) =>
    item.itemEnabled
    && /127\.0\.0\.1/i.test(`${item.itemName} ${item.itemValue}`)
    && /msedge\.exe/i.test(`${item.itemName} ${item.itemValue}`)
  );
  if (!edge?.itemValue) {
    throw new Error("OBS could not see the Edge demo window for Window Capture.");
  }
  return edge.itemValue;
}

async function configureEdgeWindowCapture(obs) {
  const scenes = await obs.request("GetSceneList");
  const sceneName = scenes.currentProgramSceneName || scenes.scenes?.[0]?.sceneName || "Scene";
  const inputName = "ERP Demo Edge Window";
  await obs.request("RemoveInput", { inputName }).catch(() => {});
  const existingItems = await obs.request("GetSceneItemList", { sceneName });
  for (const item of existingItems.sceneItems || []) {
    await obs.request("RemoveSceneItem", {
      sceneName,
      sceneItemId: item.sceneItemId,
    }).catch(() => {});
  }

  await obs.request("CreateInput", {
    sceneName,
    inputName,
    inputKind: "window_capture",
    inputSettings: {
      client_area: true,
      cursor: true,
      method: 2,
    },
    sceneItemEnabled: true,
  });

  const windowValue = await findEdgeWindowValue(obs, inputName);
  await obs.request("SetInputSettings", {
    inputName,
    inputSettings: {
      window: windowValue,
      client_area: true,
      cursor: true,
      method: 2,
    },
    overlay: true,
  });

  const itemList = await obs.request("GetSceneItemList", { sceneName });
  const sceneItem = itemList.sceneItems.find((item) => item.sourceName === inputName);
  if (sceneItem) {
    await obs.request("SetSceneItemTransform", {
      sceneName,
      sceneItemId: sceneItem.sceneItemId,
      sceneItemTransform: {
        boundsType: "OBS_BOUNDS_SCALE_INNER",
        boundsWidth: 1920,
        boundsHeight: 1080,
        positionX: 0,
        positionY: 0,
      },
    });
  }
  await obs.request("SetCurrentProgramScene", { sceneName });
  return inputName;
}

async function verifyCaptureNotBlack(obs, sourceName) {
  await sleep(1500);
  const screenshot = await obs.request("GetSourceScreenshot", {
    sourceName,
    imageFormat: "png",
    imageWidth: 640,
    imageHeight: 360,
  });
  const base64 = screenshot.imageData.replace(/^data:image\/png;base64,/, "");
  const screenshotPath = path.join(DOWNLOADS, "erp_ai_demo_capture_probe.png");
  await writeFile(screenshotPath, Buffer.from(base64, "base64"));

  const checker = spawn(process.execPath, ["-c", `
from PIL import Image, ImageStat
import sys
img = Image.open(sys.argv[1]).convert("L")
stat = ImageStat.Stat(img)
print(round(stat.mean[0], 2))
sys.exit(0 if stat.mean[0] > 8 else 2)
`, screenshotPath], { stdio: ["ignore", "pipe", "inherit"] });

  const chunks = [];
  checker.stdout.on("data", (chunk) => chunks.push(chunk));
  const code = await new Promise((resolve) => checker.on("exit", resolve));
  const mean = Buffer.concat(chunks).toString().trim();
  if (code !== 0) {
    throw new Error(`OBS capture still looks black. Screenshot mean brightness: ${mean}`);
  }
  console.log(`OBS capture brightness check passed: ${mean}`);
}

async function newestMp4Since(startedAt) {
  const files = await readdir(DOWNLOADS);
  const candidates = [];
  for (const file of files) {
    if (!file.toLowerCase().endsWith(".mp4")) continue;
    const fullPath = path.join(DOWNLOADS, file);
    const info = await stat(fullPath);
    if (info.mtimeMs >= startedAt - 2000) {
      candidates.push({ fullPath, mtimeMs: info.mtimeMs, size: info.size });
    }
  }
  candidates.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return candidates[0] || null;
}

async function main() {
  const startedAt = Date.now();
  const obsProcess = startObs();
  const obs = await connectObsWebSocket();

  await sleep(2500);
  await prepareDemoWindow();
  const captureSource = await configureEdgeWindowCapture(obs);
  await verifyCaptureNotBlack(obs, captureSource);
  await obs.request("StartRecord");
  await sleep(3000);

  try {
    await runDemoCommand(["--skip-open"]);
  } finally {
    let outputPath = null;
    try {
      const response = await obs.request("StopRecord");
      outputPath = response.outputPath || null;
    } finally {
      obs.close();
      await sleep(2000);
      obsProcess.kill();
    }

    const recorded = outputPath
      ? { fullPath: outputPath }
      : await newestMp4Since(startedAt);
    if (!recorded?.fullPath) {
      throw new Error("OBS stopped, but no MP4 recording was found in Downloads.");
    }

    if (recorded.fullPath !== FINAL_FILE) {
      await rename(recorded.fullPath, FINAL_FILE).catch(async () => {
        const timestamp = new Date().toISOString().replace(/[:.]/g, "-");
        await rename(recorded.fullPath, path.join(DOWNLOADS, `ERP_AI_Assistant_POC_Demo_${timestamp}.mp4`));
      });
    }
    console.log(`Saved recording: ${FINAL_FILE}`);
  }
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
