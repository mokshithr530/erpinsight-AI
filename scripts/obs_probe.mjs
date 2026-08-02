function createObsClient(url) {
  const ws = new WebSocket(url);
  let requestId = 1;
  const pending = new Map();
  ws.addEventListener("message", (event) => {
    const message = JSON.parse(event.data);
    if (message.op === 0) {
      ws.send(JSON.stringify({ op: 1, d: { rpcVersion: message.d.rpcVersion || 1 } }));
      return;
    }
    if (message.op === 7) {
      const p = pending.get(message.d.requestId);
      if (!p) return;
      pending.delete(message.d.requestId);
      if (message.d.requestStatus?.result) p.resolve(message.d.responseData || {});
      else p.reject(new Error(message.d.requestStatus?.comment || "OBS request failed"));
    }
  });
  return new Promise((resolve, reject) => {
    ws.addEventListener("open", () => {
      const client = {
        request(requestType, requestData = {}) {
          const requestIdString = String(requestId++);
          ws.send(JSON.stringify({ op: 6, d: { requestType, requestId: requestIdString, requestData } }));
          return new Promise((resolveRequest, rejectRequest) => {
            pending.set(requestIdString, { resolve: resolveRequest, reject: rejectRequest });
          });
        },
        close() {
          ws.close();
        },
      };
      setTimeout(() => resolve(client), 500);
    });
    ws.addEventListener("error", reject);
  });
}

const obs = await createObsClient("ws://127.0.0.1:4455");
console.log(JSON.stringify(await obs.request("GetInputKindList"), null, 2));
console.log(JSON.stringify(await obs.request("GetSceneList"), null, 2));
obs.close();
