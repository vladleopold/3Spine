import { test } from "node:test";
import assert from "node:assert/strict";
import worker from "../worker/src/index.js";

const env = { GITHUB_TOKEN: "test-token" };

function githubMock(paths) {
  return async (url) => {
    const u = new URL(String(url));
    const value = paths[u.pathname + u.search] ?? paths[u.pathname];
    if (value === undefined) {
      return new Response(JSON.stringify({ message: "missing mock: " + u.pathname + u.search }), {
        status: 404,
        headers: { "Content-Type": "application/json" },
      });
    }
    return new Response(JSON.stringify(value), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
}

test("status остаётся ready после коммита другой параллельной задачи", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = githubMock({
    "/repos/vladleopold/3Spine/branches/results": {
      commit: { sha: "new-head", commit: { message: "результат другой задачи" } },
    },
    "/repos/vladleopold/3Spine/git/trees/new-head?recursive=1": {
      tree: [
        { type: "blob", path: "history/convert-needed.zip", sha: "own-blob", size: 2 },
        { type: "blob", path: "history/convert-other.zip", sha: "other-blob", size: 2 },
      ],
    },
  });

  const response = await worker.fetch(
    new Request("https://broker.example/status?job=convert-needed"), env
  );
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { ready: true, raw: false });
  assert.match(response.headers.get("cache-control"), /no-store/);
});

test("download отдаёт архив задачи, даже когда HEAD уже принадлежит другой", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = githubMock({
    "/repos/vladleopold/3Spine/branches/results": {
      commit: { sha: "new-head", commit: { message: "результат другой задачи" } },
    },
    "/repos/vladleopold/3Spine/git/trees/new-head?recursive=1": {
      tree: [{ type: "blob", path: "history/convert-needed.zip", sha: "own-blob", size: 2 }],
    },
    "/repos/vladleopold/3Spine/git/blobs/own-blob": {
      content: Buffer.from("ok").toString("base64"),
    },
  });

  const response = await worker.fetch(
    new Request("https://broker.example/download?job=convert-needed"), env
  );
  assert.equal(response.status, 200);
  assert.equal(Buffer.from(await response.arrayBuffer()).toString(), "ok");
});

test("сырой архив показывает прогресс, но не завершённость", async (t) => {
  const originalFetch = globalThis.fetch;
  t.after(() => { globalThis.fetch = originalFetch; });
  globalThis.fetch = githubMock({
    "/repos/vladleopold/3Spine/branches/results": {
      commit: { sha: "raw-head", commit: { message: "raw spine pairs" } },
    },
    "/repos/vladleopold/3Spine/git/trees/raw-head?recursive=1": {
      tree: [{ type: "blob", path: "history/convert-needed-raw.zip", sha: "raw-blob", size: 2 }],
    },
  });

  const response = await worker.fetch(
    new Request("https://broker.example/status?job=convert-needed"), env
  );
  assert.deepEqual(await response.json(), { ready: false, raw: true });
});
