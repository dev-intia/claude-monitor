"use strict";
// Lets claude-monitor open a session tab in this exact window, without starting a process.
// Each window listens on its own loopback port and publishes port and token in a file only
// this user can read. It opens session tabs and nothing else: no paths, no prompts.
const vscode = require("vscode");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const OPEN_SESSION = "claude-vscode.primaryEditor.open";
const SESSION_ID = /^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$/;
const MAX_BODY_BYTES = 1024;
const WINDOWS_DIR = path.join(os.homedir(), ".claude-monitor", "windows");
const WINDOW_FILE = path.join(WINDOWS_DIR, `${process.pid}.json`);

let server = null;
let details = null;

function isAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return error.code === "EPERM";
  }
}

// A window that crashed could not remove its own file.
function removeDeadWindows() {
  for (const name of fs.readdirSync(WINDOWS_DIR)) {
    if (!name.endsWith(".json")) continue;
    const file = path.join(WINDOWS_DIR, name);
    try {
      if (!isAlive(JSON.parse(fs.readFileSync(file, "utf8")).pid)) fs.unlinkSync(file);
    } catch {
      // Being written by another window, or already gone.
    }
  }
}

function publish() {
  const folders = (vscode.workspace.workspaceFolders || []).map((folder) => folder.uri.fsPath);
  if (!folders.length) {
    fs.rmSync(WINDOW_FILE, { force: true });
    return;
  }
  const temporary = WINDOW_FILE + ".tmp";
  fs.writeFileSync(temporary, JSON.stringify({ pid: process.pid, folders, ...details }), { mode: 0o600 });
  fs.renameSync(temporary, WINDOW_FILE);
}

function tokenMatches(header, token) {
  const given = Buffer.from(String(header || ""));
  const expected = Buffer.from(`Bearer ${token}`);
  return given.length === expected.length && crypto.timingSafeEqual(given, expected);
}

function reply(response, status) {
  response.writeHead(status, { "Content-Length": 0, Connection: "close" });
  response.end();
}

async function openSession(sid, response) {
  // Checked first: without the command the monitor must know, and use its fallback.
  if (!(await vscode.commands.getCommands(true)).includes(OPEN_SESSION)) {
    reply(response, 404);
    return;
  }
  reply(response, 202);
  vscode.commands.executeCommand(OPEN_SESSION, sid).then(undefined, (error) => console.error(error));
}

function handle(request, response, token) {
  // The monitor's server never sends Origin; a web page always does.
  if (request.headers.origin !== undefined) return reply(response, 403);
  if (!tokenMatches(request.headers.authorization, token)) return reply(response, 401);
  if (request.method !== "POST" || request.url !== "/open") return reply(response, 404);
  const chunks = [];
  let size = 0;
  request.on("data", (chunk) => {
    size += chunk.length;
    if (size <= MAX_BODY_BYTES) chunks.push(chunk);
  });
  request.on("end", () => {
    let sid = null;
    try {
      if (size <= MAX_BODY_BYTES) sid = JSON.parse(Buffer.concat(chunks).toString("utf8")).sid;
    } catch {
      sid = null;
    }
    if (typeof sid !== "string" || !SESSION_ID.test(sid)) return reply(response, 400);
    openSession(sid, response).catch(() => reply(response, 500));
  });
}

function activate(context) {
  fs.mkdirSync(WINDOWS_DIR, { recursive: true });
  removeDeadWindows();
  const token = crypto.randomBytes(32).toString("hex");
  server = http.createServer((request, response) => handle(request, response, token));
  server.listen(0, "127.0.0.1", () => {
    details = { port: server.address().port, token };
    publish();
  });
  context.subscriptions.push(vscode.workspace.onDidChangeWorkspaceFolders(() => {
    if (details) publish();
  }));
}

function deactivate() {
  fs.rmSync(WINDOW_FILE, { force: true });
  if (server) server.close();
}

module.exports = { activate, deactivate };
