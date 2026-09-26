import {
  ButtonItem,
  DialogButton,
  DropdownItem,
  Focusable,
  PanelSection,
  PanelSectionRow,
  staticClasses,
  TextField,
  ToggleField,
} from "@decky/ui";
import { callable, definePlugin } from "@decky/api";
import { useEffect, useMemo, useState } from "react";
import { FaTerminal } from "react-icons/fa";
import qrcode from "qrcode-generator";
import { disposeSidebarTab, initSidebarTab, SidebarTabToggle } from "./sidebarTab";
import { startAutoUpdate, UpdateSection } from "./update";

// ── backend callables ──────────────────────────────────────────────────────────

const startSession = callable<
  [string, string],
  { success: boolean; url?: string; error?: string }
>("start_session");
const stopSession = callable<[], { success: boolean }>("stop_session");
const getStatus = callable<
  [],
  {
    status: string;
    url?: string;
    working_dir: string;
    resume_id?: string;
    error?: string;
    skill?: string | null;
  }
>("get_status");
const listDirs = callable<[], { dirs: string[] }>("list_dirs");

const captureScreenshot = callable<
  [boolean],
  { success: boolean; path?: string; thumbnail?: string; error?: string }
>("capture_screenshot");
const getScreenState = callable<[], { thumbnail: string | null }>("get_screen_state");

const sendKey = callable<[string], { success: boolean; error?: string }>("send_key");
const sendText = callable<[string], { success: boolean; error?: string }>("send_text");
const sendMouseClick = callable<[number], { success: boolean; error?: string }>("send_mouse_click");

const getAuth = callable<
  [],
  { logged_in: boolean; email?: string; org?: string; plan?: string; error?: string }
>("get_auth");
const startLogin = callable<[], { success: boolean; url?: string; error?: string }>("start_login");
const submitLoginCode = callable<
  [string],
  { success: boolean; email?: string; error?: string }
>("submit_login_code");
const cancelLogin = callable<[], { success: boolean }>("cancel_login");

const listSessions = callable<
  [],
  { sessions: MachineSession[]; error?: string }
>("list_sessions");

// ── types ──────────────────────────────────────────────────────────────────────

const SESSION_STATUSES = ["stopped", "starting", "running", "error"] as const;

type SessionStatus = (typeof SESSION_STATUSES)[number];

interface MachineSession {
  id: string;
  short_id: string;
  cwd: string;
  label: string;
  preview: string;
  mtime: number;
  live: boolean;
  current: boolean;
}

const STATUS_COLOR: Record<SessionStatus, string> = {
  stopped: "#888",
  starting: "#f0a500",
  running: "#4caf50",
  error: "#f44336",
};

const QUICK_KEYS = [
  { label: "Esc", key: "escape" },
  { label: "Enter", key: "Return" },
  { label: "Space", key: "space" },
  { label: "Tab", key: "Tab" },
];

/** The status crosses the RPC boundary as a bare string, so a backend that
 *  gains a state the UI doesn't know about must not slip past the union. */
function parseStatus(value: unknown): SessionStatus | null {
  return SESSION_STATUSES.includes(value as SessionStatus) ? (value as SessionStatus) : null;
}

/** Callables reject on transport/serialisation failures as well as returning
 *  {success: false}, and both paths have to end up in the same banner. */
function errorText(e: unknown, fallback: string): string {
  const msg = e instanceof Error ? e.message : typeof e === "string" ? e : "";
  return msg.trim() || fallback;
}

/** Polls run every few seconds, so a transient failure must not tear the panel
 *  down or bury the banner a user-initiated action just wrote. */
function logPollFailure(e: unknown) {
  console.error("[decky-claude] background sync failed:", e);
}

function relativeTime(epochSeconds: number): string {
  const mins = Math.max(0, Math.floor(Date.now() / 1000 - epochSeconds) / 60);
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.floor(mins)}m ago`;
  if (mins < 60 * 24) return `${Math.floor(mins / 60)}h ago`;
  return `${Math.floor(mins / 60 / 24)}d ago`;
}

/** QR as a single scaled bitmap.
 *
 *  An OAuth URL is ~450 chars, which needs a 73x73 matrix — rendering that as
 *  a div grid would be 5000+ DOM nodes for CEF to lay out. createDataURL emits
 *  one GIF instead; upscaling it with pixelated rendering keeps the module
 *  edges hard, which is what the phone camera needs.
 */
function QrCode({ text, size = 260 }: { text: string; size?: number }) {
  // Encoding is expensive enough to be felt on a keystroke in the sibling
  // login-code field, and the bitmap only depends on the URL.
  const src = useMemo(() => {
    // Type 0 auto-picks the smallest version that fits; "L" adds the least
    // redundancy, so the matrix stays as small (and each module as large) as
    // possible for a long URL.
    const qr = qrcode(0, "L");
    qr.addData(text);
    qr.make();
    return qr.createDataURL(8, 1);
  }, [text]);

  return (
    <div style={{ background: "#fff", padding: 10, borderRadius: 6, margin: "0 auto" }}>
      <img
        src={src}
        width={size}
        height={size}
        style={{ display: "block", imageRendering: "pixelated" }}
        alt="Sign-in QR code"
      />
    </div>
  );
}

// ── component ──────────────────────────────────────────────────────────────────

function Content() {
  // session
  const [status, setStatus] = useState<SessionStatus>("stopped");
  const [sessionUrl, setSessionUrl] = useState<string | null>(null);
  const [workingDir, setWorkingDir] = useState("");
  const [dirs, setDirs] = useState<string[]>([]);
  const [sessionLoading, setSessionLoading] = useState(false);
  // Which action sessionLoading is for — `status` flips to "starting"
  // (which isRunning treats as running) almost immediately after a start is
  // kicked off, so isRunning can't be used to tell "starting" from "stopping".
  const [sessionAction, setSessionAction] = useState<"start" | "stop" | null>(null);
  const [sessionError, setSessionError] = useState<string | null>(null);
  const [urlCopied, setUrlCopied] = useState(false);
  const [skill, setSkill] = useState<string | null>(null);

  // auth
  const [loggedIn, setLoggedIn] = useState<boolean | null>(null);
  const [account, setAccount] = useState<string | null>(null);
  const [plan, setPlan] = useState<string | null>(null);
  const [loginUrl, setLoginUrl] = useState<string | null>(null);
  const [loginBusy, setLoginBusy] = useState(false);
  const [loginCode, setLoginCode] = useState("");
  const [loginError, setLoginError] = useState<string | null>(null);

  // other sessions on this machine
  const [machineSessions, setMachineSessions] = useState<MachineSession[]>([]);
  // "" = start a fresh session; otherwise the transcript id to resume
  const [resumeId, setResumeId] = useState("");

  // screen
  const [thumbnail, setThumbnail] = useState<string | null>(null);
  const [captureLoading, setCaptureLoading] = useState(false);
  const [captureError, setCaptureError] = useState<string | null>(null);
  // gamescope excludes the Steam overlay (QAM, notifications) from a capture
  // by default, same as the physical screenshot button — this opts in.
  const [includeSteamUi, setIncludeSteamUi] = useState(false);

  // input
  const [typeText, setTypeText] = useState("");
  const [inputFeedback, setInputFeedback] = useState<{ msg: string; ok: boolean } | null>(null);

  // ── init ──
  useEffect(() => {
    listDirs()
      .then((r) => {
        setDirs(r.dirs);
        // The backend resolves the real home; adopt its first entry as default
        // rather than assuming /home/deck on the frontend.
        setWorkingDir((cur) => cur || r.dirs[0] || "");
      })
      .catch((e) => setSessionError(errorText(e, "Could not list working directories")));
    syncStatus();
    syncAuth();
    syncMachineSessions();
    getScreenState()
      .then((r) => { if (r.thumbnail) setThumbnail(r.thumbnail); })
      .catch(logPollFailure);
  }, []);

  useEffect(() => {
    const id = setInterval(syncStatus, 3000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    const id = setInterval(syncMachineSessions, 10000);
    return () => clearInterval(id);
  }, []);

  async function syncAuth() {
    try {
      const r = await getAuth();
      setLoggedIn(r.logged_in);
      setAccount(r.email ?? null);
      setPlan(r.plan ?? null);
      if (r.logged_in) {
        setLoginUrl(null);
        setLoginError(null);
      }
    } catch (e) {
      logPollFailure(e);
    }
  }

  async function syncMachineSessions() {
    try {
      const r = await listSessions();
      setMachineSessions(r.sessions ?? []);
    } catch (e) {
      logPollFailure(e);
    }
  }

  async function handleStartLogin() {
    setLoginBusy(true);
    setLoginError(null);
    try {
      const r = await startLogin();
      if (r.success && r.url) setLoginUrl(r.url);
      else setLoginError(r.error ?? "Could not start login");
    } catch (e) {
      setLoginError(errorText(e, "Could not start login"));
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleSubmitCode() {
    if (!loginCode.trim()) return;
    setLoginBusy(true);
    setLoginError(null);
    try {
      const r = await submitLoginCode(loginCode);
      setLoginCode("");
      if (r.success) {
        setLoginUrl(null);
        await syncAuth();
      } else {
        setLoginError(r.error ?? "Login failed");
      }
    } catch (e) {
      // The code is single-use once the backend has seen it, so clear it here
      // too and let the user paste a fresh one.
      setLoginCode("");
      setLoginError(errorText(e, "Login failed"));
    } finally {
      setLoginBusy(false);
    }
  }

  async function handleCancelLogin() {
    try {
      await cancelLogin();
    } catch (e) {
      logPollFailure(e);
    }
    // Cancelling is a UI retreat: drop the login state either way, otherwise a
    // failed cancel strands the user on a QR code they can no longer use.
    setLoginUrl(null);
    setLoginCode("");
    setLoginError(null);
  }

  async function syncStatus() {
    try {
      const r = await getStatus();
      const next = parseStatus(r.status);
      if (next) {
        setStatus(next);
        // Mirror the backend's error exactly, including clearing it once the
        // backend has — otherwise a resolved/stale error sticks in the panel
        // forever since this poll never ran the "clear" branch.
        setSessionError(r.error ?? null);
      } else {
        setStatus("error");
        setSessionError(`Backend reported an unknown status: ${String(r.status)}`);
      }
      setSessionUrl(r.url ?? null);
      setSkill(r.skill ?? null);
      // A session started before the panel was opened still has to show what it
      // is resuming, so the backend's value wins while one is running.
      if (next === "running" || next === "starting") setResumeId(r.resume_id ?? "");
    } catch (e) {
      logPollFailure(e);
    }
  }

  // ── session ──
  async function handleStartSession() {
    setSessionLoading(true);
    setSessionAction("start");
    setSessionError(null);
    try {
      const r = await startSession(workingDir, resumeId);
      if (r.success) {
        setStatus("running");
        setSessionUrl(r.url ?? null);
        setSessionError(null);
      } else {
        setStatus("error");
        setSessionError(r.error ?? "Failed to start session");
      }
    } catch (e) {
      setStatus("error");
      setSessionError(errorText(e, "Failed to start session"));
    } finally {
      setSessionLoading(false);
      setSessionAction(null);
    }
  }

  async function handleStopSession() {
    setSessionLoading(true);
    setSessionAction("stop");
    try {
      await stopSession();
      setStatus("stopped");
      setSessionUrl(null);
      setSessionError(null);
      setResumeId("");
    } catch (e) {
      // The process may well still be alive, so leave the status alone and let
      // the poll report what actually happened.
      setSessionError(errorText(e, "Failed to stop session"));
    } finally {
      setSessionLoading(false);
      setSessionAction(null);
    }
  }

  function copyUrl() {
    if (!sessionUrl) return;
    const el = document.createElement("textarea");
    el.value = sessionUrl;
    document.body.appendChild(el);
    el.select();
    document.execCommand("copy");
    document.body.removeChild(el);
    setUrlCopied(true);
    setTimeout(() => setUrlCopied(false), 2000);
  }

  // ── screen ──
  async function handleCapture() {
    setCaptureLoading(true);
    setCaptureError(null);
    try {
      const r = await captureScreenshot(includeSteamUi);
      if (r.success && r.thumbnail) {
        setThumbnail(r.thumbnail);
      } else if (!r.success) {
        setCaptureError(r.error ?? "Capture failed");
      }
    } catch (e) {
      setCaptureError(errorText(e, "Capture failed"));
    } finally {
      setCaptureLoading(false);
    }
  }

  // ── input ──
  function showFeedback(msg: string, ok: boolean) {
    setInputFeedback({ msg, ok });
    setTimeout(() => setInputFeedback(null), 2000);
  }

  async function handleKey(key: string) {
    try {
      const r = await sendKey(key);
      showFeedback(r.success ? `Sent: ${key}` : (r.error ?? "Failed"), r.success);
    } catch (e) {
      showFeedback(errorText(e, "Failed"), false);
    }
  }

  async function handleType() {
    if (!typeText) return;
    try {
      const r = await sendText(typeText);
      showFeedback(r.success ? "Typed!" : (r.error ?? "Failed"), r.success);
      if (r.success) setTypeText("");
    } catch (e) {
      showFeedback(errorText(e, "Failed"), false);
    }
  }

  async function handleClick(button: number) {
    try {
      const r = await sendMouseClick(button);
      showFeedback(r.success ? "Clicked" : (r.error ?? "Failed"), r.success);
    } catch (e) {
      showFeedback(errorText(e, "Failed"), false);
    }
  }

  const isRunning = status === "running" || status === "starting";
  // A live session is already attached to a claude process; resuming it a
  // second time would run two clients against one transcript.
  const resumable = machineSessions.filter((s) => !s.live);
  const resumeSession = machineSessions.find((s) => s.id === resumeId) ?? null;
  const statusColor = STATUS_COLOR[status] ?? "#888";
  const statusLabel =
    status === "starting" ? "Starting…" : status.charAt(0).toUpperCase() + status.slice(1);

  return (
    <>
      {/* ── Sign in ────────────────────────────────────────────────────── */}
      {loggedIn === false && (
        <PanelSection title="Sign in to Claude Code">
          {!loginUrl && (
            <>
              <PanelSectionRow>
                <div style={{ fontSize: 11, color: "#f0a500", lineHeight: 1.4 }}>
                  Not signed in — a session can't start until you do.
                </div>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" disabled={loginBusy} onClick={handleStartLogin}>
                  {loginBusy ? "Starting…" : "Sign in"}
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}

          {loginUrl && (
            <>
              <PanelSectionRow>
                <div style={{ fontSize: 11, color: "#aaa", lineHeight: 1.4 }}>
                  Scan with your phone, approve the login, then paste the code below.
                </div>
              </PanelSectionRow>
              <PanelSectionRow>
                <QrCode text={loginUrl} />
              </PanelSectionRow>
              <PanelSectionRow>
                <TextField
                  label="Code from browser"
                  value={loginCode}
                  onChange={(e) => setLoginCode(e.target.value)}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem
                  layout="below"
                  disabled={loginBusy || !loginCode.trim()}
                  onClick={handleSubmitCode}
                >
                  {loginBusy ? "Signing in…" : "Submit code"}
                </ButtonItem>
              </PanelSectionRow>
              <PanelSectionRow>
                <ButtonItem layout="below" onClick={handleCancelLogin}>
                  Cancel
                </ButtonItem>
              </PanelSectionRow>
            </>
          )}

          {loginError && (
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#f44336", wordBreak: "break-word" }}>
                {loginError}
              </div>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      {/* ── Remote Session ─────────────────────────────────────────────── */}
      <PanelSection title="Claude Code Remote">
        <PanelSectionRow>
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <div
              style={{
                width: 10, height: 10, borderRadius: "50%",
                background: statusColor, flexShrink: 0,
                boxShadow: `0 0 6px ${statusColor}`,
              }}
            />
            <span style={{ color: statusColor, fontWeight: 600 }}>{statusLabel}</span>
          </div>
        </PanelSectionRow>

        {loggedIn && account && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#888" }}>
              {account}{plan ? ` · ${plan}` : ""}
            </div>
          </PanelSectionRow>
        )}

        {isRunning && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: skill ? "#4caf50" : "#f0a500" }}>
              {skill
                ? `Skill loaded: ${skill}`
                : "steam-debugger skill not found (~/.claude/skills)"}
            </div>
          </PanelSectionRow>
        )}

        {sessionUrl && (
          <>
            <PanelSectionRow>
              <div style={{
                fontSize: 11, wordBreak: "break-all", color: "#5ba3f5",
                background: "rgba(91,163,245,0.08)", borderRadius: 6,
                padding: "6px 8px", lineHeight: 1.4,
              }}>
                {sessionUrl}
              </div>
            </PanelSectionRow>
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={copyUrl}>
                {urlCopied ? "Copied!" : "Copy Session URL"}
              </ButtonItem>
            </PanelSectionRow>
            <PanelSectionRow>
              <div style={{ fontSize: 11, color: "#aaa" }}>
                Claude app → Code tab → connect. Claude will screenshot automatically when you ask about the game.
              </div>
            </PanelSectionRow>
          </>
        )}

        {status === "starting" && !sessionUrl && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#f0a500" }}>Waiting for session URL…</div>
          </PanelSectionRow>
        )}

        {sessionError && (
          <PanelSectionRow>
            <div style={{
              fontSize: 11, color: "#f44336", wordBreak: "break-all",
              background: "rgba(244,67,54,0.08)", borderRadius: 6, padding: "6px 8px",
            }}>
              {sessionError}
            </div>
          </PanelSectionRow>
        )}

        {!isRunning && (
          <>
            <PanelSectionRow>
              <DropdownItem
                label="Session"
                description={
                  resumeSession
                    ? resumeSession.preview || `in ${resumeSession.cwd}`
                    : "Starts a fresh conversation"
                }
                rgOptions={[
                  { data: "", label: "New session" },
                  ...resumable.map((s) => ({
                    data: s.id,
                    label: `${s.label} · ${relativeTime(s.mtime)}`,
                  })),
                ]}
                selectedOption={resumeId}
                onChange={(opt) => setResumeId(opt.data)}
              />
            </PanelSectionRow>

            {/* Resuming replays a transcript, and that only works in the
                directory it was recorded in — so the backend picks the cwd. */}
            {!resumeId && (
              <PanelSectionRow>
                <DropdownItem
                  label="Working Directory"
                  description={workingDir}
                  rgOptions={dirs.map((d) => ({ data: d, label: d }))}
                  selectedOption={workingDir}
                  onChange={(opt) => setWorkingDir(opt.data)}
                />
              </PanelSectionRow>
            )}
          </>
        )}

        <PanelSectionRow>
          <ButtonItem
            layout="below"
            onClick={isRunning ? handleStopSession : handleStartSession}
            disabled={sessionLoading}
          >
            {sessionLoading
              ? sessionAction === "stop" ? "Stopping…" : "Starting…"
              : isRunning ? "Stop Session"
              : resumeId ? "Resume Session"
              : "Start Remote Session"}
          </ButtonItem>
        </PanelSectionRow>

        {sessionError?.toLowerCase().includes("not found") && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#aaa", lineHeight: 1.6 }}>
              In Desktop Mode, run:
              <code style={{
                display: "block", marginTop: 4,
                background: "rgba(255,255,255,0.07)",
                borderRadius: 4, padding: "2px 6px",
              }}>
                npm install -g @anthropic-ai/claude-code
              </code>
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      {/* ── Sessions on this machine ───────────────────────────────────── */}
      {machineSessions.length > 0 && (
        <PanelSection title="Sessions on this device">
          {/* Steam marks D-pad focus with a .gpfocus class (not :focus), and an
              inline background would silently beat it — so all row colours
              live here, and focus is declared last so it wins over selected. */}
          <style>{`
            button.DialogButton.decky-claude-session-row {
              background: rgba(255,255,255,0.04) !important;
              border: 1px solid transparent !important;
            }
            button.DialogButton.decky-claude-session-row[data-selected="true"] {
              background: rgba(91,163,245,0.22) !important;
              border-color: rgba(91,163,245,0.85) !important;
            }
            button.DialogButton.decky-claude-session-row.gpfocus,
            button.DialogButton.decky-claude-session-row:hover {
              background: rgb(70,77,88) !important;
              border-color: rgba(255,255,255,0.9) !important;
            }
            button.DialogButton.decky-claude-session-row.gpfocus[data-selected="true"],
            button.DialogButton.decky-claude-session-row[data-selected="true"]:hover {
              background: rgba(91,163,245,0.45) !important;
              border-color: #fff !important;
            }
          `}</style>
          {machineSessions.map((s) => {
            const selected = s.id === resumeId;
            const selectable = !s.live && !isRunning;
            return (
              <PanelSectionRow key={s.id}>
                <DialogButton
                  // Rows are buttons rather than divs so the D-pad can reach
                  // them: the Quick Access panel scrolls to whatever has focus,
                  // and unfocusable content is a dead end for gamepad users.
                  onClick={() => selectable && setResumeId(s.id)}
                  className="decky-claude-session-row"
                  data-selected={selected}
                  style={{
                    width: "100%", minWidth: 0, padding: "6px 8px",
                    textAlign: "left", display: "flex", alignItems: "center", gap: 8,
                  }}
                >
                  <div
                    style={{
                      width: 8, height: 8, borderRadius: "50%", flexShrink: 0,
                      background: s.live ? "#4caf50" : "#555",
                      boxShadow: s.live ? "0 0 6px #4caf50" : "none",
                    }}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{
                      fontSize: 12, fontWeight: s.current ? 700 : 500,
                      color: s.current ? "#5ba3f5" : "#ddd",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {s.label}{s.current ? " (this panel)" : ""}
                    </div>
                    {s.preview && (
                      <div style={{
                        fontSize: 10, color: "#9aa",
                        overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                      }}>
                        {s.preview}
                      </div>
                    )}
                    <div style={{
                      fontSize: 10, color: "#777",
                      overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap",
                    }}>
                      {s.live ? "running" : relativeTime(s.mtime)} · {s.short_id}
                      {/* Once the resume actually started, "will resume" is
                          just wrong — s.current already says it's live here. */}
                      {selected && !isRunning ? " · will resume" : ""}
                    </div>
                  </div>
                  {/* Text content, not CSS — stays visible even if DialogButton's
                      own focus/hover styling wins the background fight above. */}
                  {selected && (
                    <div
                      aria-hidden
                      style={{ fontSize: 14, color: "#5ba3f5", flexShrink: 0, lineHeight: 1 }}
                    >
                      ✓
                    </div>
                  )}
                </DialogButton>
              </PanelSectionRow>
            );
          })}
          <PanelSectionRow>
            <div style={{ fontSize: 10, color: "#666", lineHeight: 1.4 }}>
              Green means a claude process is live in that directory — open those
              from the Claude app. Pick any other one to resume it here, then hit
              Resume Session.
            </div>
          </PanelSectionRow>
        </PanelSection>
      )}

      {/* ── Screen Preview ──────────────────────────────────────────────── */}
      <PanelSection title="Screen Preview">
        {thumbnail && (
          <PanelSectionRow>
            <img
              src={`data:image/png;base64,${thumbnail}`}
              style={{
                width: "100%", borderRadius: 6, display: "block",
                border: "1px solid rgba(255,255,255,0.1)",
              }}
              alt="Last capture"
            />
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <ToggleField
            label="Include Steam UI"
            description="Also capture the Quick Access Menu / overlay, not just the game"
            checked={includeSteamUi}
            onChange={setIncludeSteamUi}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem layout="below" onClick={handleCapture} disabled={captureLoading}>
            {captureLoading ? "Capturing…" : "Capture Screen"}
          </ButtonItem>
        </PanelSectionRow>

        {captureError && (
          <PanelSectionRow>
            <div style={{ fontSize: 11, color: "#f44336" }}>{captureError}</div>
          </PanelSectionRow>
        )}

        <PanelSectionRow>
          <div style={{ fontSize: 11, color: "#555", lineHeight: 1.4 }}>
            For your own preview — Claude captures on its own when you ask it
            something. By default a capture is the game/desktop frame only,
            same as the physical screenshot button; the overlay shown here
            isn't part of it unless "Include Steam UI" is on.
          </div>
        </PanelSectionRow>
      </PanelSection>

      {/* ── Manual Input ────────────────────────────────────────────────── */}
      <PanelSection title="Manual Input">
        <PanelSectionRow>
          <div style={{
            fontSize: 11, color: "#f0a500",
            background: "rgba(240,165,0,0.08)",
            borderRadius: 6, padding: "5px 8px",
          }}>
            ⚠ Input goes directly to the focused app
          </div>
        </PanelSectionRow>

        {/* Plain <button>/<input> are invisible to Steam's gamepad focus
            system: the D-pad cannot enter them, and because the Quick Access
            panel scrolls by following focus, everything below them becomes
            unreachable. Use Decky's focusable primitives instead. */}
        <PanelSectionRow>
          <Focusable style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
            {QUICK_KEYS.map(({ label, key }) => (
              <DialogButton
                key={key}
                onClick={() => handleKey(key)}
                style={{ flex: "1 1 auto", minWidth: 0, padding: "6px 10px", fontSize: 12 }}
              >
                {label}
              </DialogButton>
            ))}
          </Focusable>
        </PanelSectionRow>

        <PanelSectionRow>
          <Focusable style={{ display: "flex", gap: 6 }}>
            {[{ label: "Left Click", btn: 1 }, { label: "Right Click", btn: 3 }].map(
              ({ label, btn }) => (
                <DialogButton
                  key={btn}
                  onClick={() => handleClick(btn)}
                  style={{ flex: 1, minWidth: 0, padding: "6px 10px", fontSize: 12 }}
                >
                  {label}
                </DialogButton>
              )
            )}
          </Focusable>
        </PanelSectionRow>

        <PanelSectionRow>
          <TextField
            label="Text to type"
            bShowClearAction
            value={typeText}
            onChange={(e) => setTypeText(e.target.value)}
          />
        </PanelSectionRow>

        <PanelSectionRow>
          <ButtonItem layout="below" disabled={!typeText} onClick={handleType}>
            Send Text
          </ButtonItem>
        </PanelSectionRow>

        {inputFeedback && (
          <PanelSectionRow>
            <div style={{
              fontSize: 11,
              color: inputFeedback.ok ? "#4caf50" : "#f44336",
            }}>
              {inputFeedback.msg}
            </div>
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Settings">
        <PanelSectionRow>
          <SidebarTabToggle />
        </PanelSectionRow>
      </PanelSection>
      {/* ── Plugin Updates (see update.tsx) ─────────────────────────────── */}
      <UpdateSection />
    </>
  );
}

// ── plugin entry ───────────────────────────────────────────────────────────────

export default definePlugin(() => {
  initSidebarTab(<Content />);
  const stopAutoUpdate = startAutoUpdate();
  return {
    name: "Claude Code",
    title: <div className={staticClasses.Title}>Claude Code</div>,
    icon: <FaTerminal />,
    content: <Content />,
    onDismount() {
      disposeSidebarTab();
      stopAutoUpdate();
    },
  };
});
