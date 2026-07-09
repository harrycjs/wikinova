import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Radio, RefreshCw, Save } from "lucide-react";

import { api } from "@/lib/api-client";
import { cn } from "@/lib/utils";

interface IMAStatus {
  enabled: boolean;
  has_credentials: boolean;
  base_url: string;
  client_id?: string | null;
  api_key?: string | null;
}

interface ObsidianStatus {
  enabled: boolean;
  vault_path: string | null;
  last_sync_at: string | null;
  file_count: number;
  mode: string;
}

interface WeixinStatus {
  connected: boolean;
  has_token: boolean;
  enabled: boolean;
}

export function ChannelsView(): JSX.Element {
  const { t } = useTranslation();
  const [tab, setTab] = useState<"overview" | "ima" | "obsidian" | "feishu" | "weixin" | "wxmp">("overview");
  const [ima, setIma] = useState<IMAStatus | null>(null);
  const [obs, setObs] = useState<ObsidianStatus | null>(null);
  const [channels, setChannels] = useState<ChannelStatus[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [syncLog, setSyncLog] = useState<string[]>([]);
  const [weixinConnected, setWeixinConnected] = useState(false);

  // Editable fields
  const [imaClientId, setImaClientId] = useState("");
  const [imaApiKey, setImaApiKey] = useState("");
  const [obsVaultPath, setObsVaultPath] = useState("");

  // Feishu config
  const [feishuEnabled, setFeishuEnabled] = useState(false);
  const [feishuAppId, setFeishuAppId] = useState("");
  const [feishuAppSecret, setFeishuAppSecret] = useState("");
  const [feishuEncryptKey, setFeishuEncryptKey] = useState("");
  const [feishuToken, setFeishuToken] = useState("");

  // WeChat config
  const [weixinEnabled, setWeixinEnabled] = useState(false);
  const [weixinToken, setWeixinToken] = useState("");
  const [weixinBaseUrl, setWeixinBaseUrl] = useState("https://ilinkai.weixin.qq.com");

  async function refresh(): Promise<void> {
    try {
      const [i, o, wxState, cfg] = await Promise.all([
        api.get<IMAStatus>("/api/ima/status"),
        api.get<ObsidianStatus>("/api/obsidian/status"),
        api.get<{ connected: boolean; has_token: boolean; enabled: boolean }>("/api/weixin/status").catch(() => ({ connected: false, has_token: false, enabled: false })),
        api.get<{ channels: Record<string, Record<string, unknown>> }>("/api/channels/config").catch(() => ({ channels: {} })),
      ]);
      setIma(i);
      setObs(o);
      setWeixinEnabled(wxState.enabled);
      setWeixinConnected(wxState.connected);
      if (i.client_id) setImaClientId(i.client_id);
      if (i.api_key) setImaApiKey(i.api_key);
      if (o.vault_path) setObsVaultPath(o.vault_path);
      // Load channel configs
      const fc = (cfg.channels || {}).feishu || {};
      setFeishuEnabled(Boolean(fc.enabled));
      if (fc.appId) setFeishuAppId(String(fc.appId));
      if (fc.appSecret) setFeishuAppSecret(String(fc.appSecret));
      if (fc.encryptKey) setFeishuEncryptKey(String(fc.encryptKey));
      if (fc.verificationToken) setFeishuToken(String(fc.verificationToken));
      // WeChat: load token from config
      if (wxState.has_token) {
        const wc = (cfg.channels || {}).weixin || {};
        if (wc.token) setWeixinToken(String(wc.token));
        if (wc.baseUrl) setWeixinBaseUrl(String(wc.baseUrl));
      }
    } catch (err) {
      setError((err as Error).message);
    }
  }

  async function loadChannelConfigs(): Promise<void> {
    try {
      const cfg = await api.get<{ channels: Record<string, Record<string, unknown>> }>("/api/channels/config");
      const fc = (cfg.channels || {}).feishu || {};
      const wc = (cfg.channels || {}).weixin || {};
      setFeishuAppId(String(fc.appId || fc.app_id || ""));
      setFeishuAppSecret(String(fc.appSecret || fc.app_secret || ""));
      setFeishuEncryptKey(String(fc.encryptKey || fc.encrypt_key || ""));
      setFeishuToken(String(fc.verificationToken || fc.verification_token || ""));
      setFeishuEnabled(Boolean(fc.enabled));
      setWeixinToken(String(wc.token || ""));
      setWeixinBaseUrl(String(wc.baseUrl || wc.base_url || "https://ilinkai.weixin.qq.com"));
      setWeixinEnabled(Boolean(wc.enabled));
    } catch (err) {
      // configs not loaded yet
    }
  }

  async function saveConfig(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const params = new URLSearchParams({
        ima_client_id: imaClientId,
        ima_api_key: imaApiKey,
        ima_enabled: String(!!imaClientId && !!imaApiKey),
        obs_vault_path: obsVaultPath,
        obs_enabled: String(!!obsVaultPath),
        feishu_enabled: String(feishuEnabled),
        feishu_app_id: feishuAppId,
        feishu_app_secret: feishuAppSecret,
        feishu_encrypt_key: feishuEncryptKey,
        feishu_verification_token: feishuToken,
        weixin_enabled: String(weixinEnabled),
        weixin_token: weixinToken,
        weixin_base_url: weixinBaseUrl,
      });
      await api.get(`/api/channels/save-config?${params.toString()}`);
      setSaved("Config saved");
      setTimeout(() => setSaved(null), 3000);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function syncNow(kind: "ima" | "obsidian"): Promise<void> {
    setBusy(true);
    setSyncLog(["Starting sync..."]);
    setError(null);
    try {
      const path = kind === "ima" ? "/api/ima/sync" : "/api/obsidian/resync";
      const data = await api.get<{ ok: boolean; log: string[]; created?: number; skipped?: number }>(path);
      setSyncLog(data.log || []);
      if (!data.ok) setError(data.log?.[0] || "Sync failed");
      await refresh();
    } catch (err) {
      setError((err as Error).message);
      setSyncLog((prev) => [...prev, `ERROR: ${(err as Error).message}`]);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    refresh();
    loadChannelConfigs();
  }, []);

  const allTabs = ["overview", "ima", "obsidian", "feishu", "weixin", "wxmp"] as const;

  return (
    <div className="jobs-mode flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-border/40 px-6 py-4">
        <Radio className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-lg font-medium">{t("channels.title")}</h1>
        <div className="ml-6 flex items-center gap-1 flex-wrap">
          {allTabs.map((name) => (
            <button
              key={name}
              type="button"
              onClick={() => setTab(name)}
              className={cn(
                "rounded px-3 py-1 text-sm capitalize",
                tab === name
                  ? "bg-muted/60 text-foreground"
                  : "text-muted-foreground hover:bg-muted/30",
              )}
            >
              {name}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={saveConfig}
          disabled={busy}
          className="ml-auto flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-sm hover:bg-muted/40 disabled:opacity-50"
        >
          <Save className="h-3.5 w-3.5" />
          {t("channels.save")}
        </button>
      </header>

      {error && (
        <div className="border-b border-destructive/30 bg-destructive/5 px-6 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      {saved && (
        <div className="border-b border-emerald-500/30 bg-emerald-500/5 px-6 py-2 text-sm text-emerald-600">
          {saved}
        </div>
      )}

      <div className="flex-1 overflow-y-auto p-6">
        {tab === "overview" && (
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            <ChannelCard title="IMA" enabled={ima?.enabled ?? false} detail={ima?.has_credentials ? "Connected" : "Not configured"} />
            <ChannelCard title="Obsidian" enabled={obs?.enabled ?? false} detail={obs?.vault_path ? `${obs.file_count} files` : "Not configured"} />
            <ChannelCard title="Feishu" enabled={feishuEnabled} detail={feishuAppId ? `App: ${feishuAppId.slice(0, 8)}...` : "Not configured"} />
            <ChannelCard title="WeChat" enabled={weixinEnabled} detail={weixinConnected ? "已连接" : weixinEnabled ? "Token missing" : "Not configured"} />
            <WxMpOverviewCard />
          </div>
        )}

        {tab === "ima" && (
          <section className="max-w-xl space-y-4">
            <h2 className="text-sm uppercase tracking-wide text-muted-foreground">IMA Configuration</h2>
            <InputField label="Client ID" value={imaClientId} onChange={setImaClientId} placeholder="e87b60bd..." />
            <InputField label="API Key" value={imaApiKey} onChange={setImaApiKey} placeholder="Your API key" type="password" />
            <StatusDot enabled={ima?.has_credentials} label={ima?.has_credentials ? "Credentials valid" : "Missing credentials"} />
            <SyncButton onClick={() => syncNow("ima")} busy={busy} enabled={ima?.enabled} />
          </section>
        )}

        {tab === "obsidian" && (
          <section className="max-w-xl space-y-4">
            <h2 className="text-sm uppercase tracking-wide text-muted-foreground">Obsidian Configuration</h2>
            <InputField label="Vault path" value={obsVaultPath} onChange={setObsVaultPath} placeholder="D:/path/to/obsidian/vault" />
            <dl className="space-y-1 text-sm">
              <InfoRow label="Mode" value={obs?.mode ?? "filesystem"} />
              <InfoRow label="Files" value={String(obs?.file_count ?? 0)} />
            </dl>
            <SyncButton onClick={() => syncNow("obsidian")} busy={busy} enabled={obs?.enabled} label="Resync vault" />
          </section>
        )}

        {tab === "feishu" && (
          <section className="max-w-xl space-y-4">
            <h2 className="text-sm uppercase tracking-wide text-muted-foreground">飞书 (Feishu) Configuration</h2>
            <p className="text-xs text-muted-foreground">
              配置飞书机器人后，可在飞书中直接与 nanobot 对话。
            </p>
            <ToggleField label="Enable Feishu" checked={feishuEnabled} onChange={setFeishuEnabled} />
            <InputField label="App ID" value={feishuAppId} onChange={setFeishuAppId} placeholder="cli_xxxxx" disabled={!feishuEnabled} />
            <InputField label="App Secret" value={feishuAppSecret} onChange={setFeishuAppSecret} placeholder="App secret" type="password" disabled={!feishuEnabled} />
            <InputField label="Encrypt Key" value={feishuEncryptKey} onChange={setFeishuEncryptKey} placeholder="Optional encrypt key" type="password" disabled={!feishuEnabled} />
            <InputField label="Verification Token" value={feishuToken} onChange={setFeishuToken} placeholder="Optional verification token" type="password" disabled={!feishuEnabled} />
            <StatusDot enabled={feishuEnabled && !!feishuAppId && !!feishuAppSecret} label={feishuEnabled && feishuAppId ? "Ready to connect" : "Credentials required"} />
            <p className="text-xs text-muted-foreground">
              1. 在飞书开放平台创建应用 → 2. 填入 App ID 和 Secret → 3. 保存配置 → 4. 重启 gateway
            </p>
          </section>
        )}

        {tab === "weixin" && (
          <WeChatTab
            connected={weixinConnected}
            enabled={weixinEnabled}
            setEnabled={setWeixinEnabled}
            token={weixinToken}
            setToken={setWeixinToken}
            baseUrl={weixinBaseUrl}
            setBaseUrl={setWeixinBaseUrl}
          />
        )}

        {tab === "wxmp" && <WxMpTab />}
      </div>
    </div>
  );
}

function InputField({ label, value, onChange, placeholder, type = "text", disabled = false }: {
  label: string; value: string; onChange: (v: string) => void; placeholder?: string; type?: string; disabled?: boolean;
}) {
  return (
    <div>
      <label className="mb-1 block text-sm font-medium">{label}</label>
      <input
        type={type}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
        className="w-full rounded border border-border/60 bg-background px-3 py-2 text-sm outline-none focus:border-foreground/40 disabled:opacity-40"
      />
    </div>
  );
}

function ToggleField({ label, checked, onChange }: { label: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-center justify-between">
      <label className="text-sm font-medium">{label}</label>
      <button
        type="button"
        onClick={() => onChange(!checked)}
        className={cn(
          "relative h-5 w-9 rounded-full transition-colors",
          checked ? "bg-foreground" : "bg-border",
        )}
      >
        <span
          className={cn(
            "absolute top-0.5 left-0.5 h-4 w-4 rounded-full bg-background transition-transform",
            checked && "translate-x-4",
          )}
        />
      </button>
    </div>
  );
}

function InfoRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between border-b border-border/40 py-1.5">
      <dt className="text-muted-foreground">{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function StatusDot({ enabled, label }: { enabled?: boolean; label: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted-foreground">
      <span className={cn("h-2 w-2 rounded-full", enabled ? "bg-emerald-500" : "bg-muted-foreground/40")} />
      {label}
    </div>
  );
}

function SyncButton({ onClick, busy, enabled, label = "Sync now" }: {
  onClick: () => void; busy: boolean; enabled?: boolean; label?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={busy || !enabled}
      className="flex items-center gap-2 rounded border border-border px-3 py-1.5 text-sm hover:bg-muted/40 disabled:opacity-50"
    >
      <RefreshCw className={cn("h-3.5 w-3.5", busy && "animate-spin")} />
      {label}
    </button>
  );
}

function WeChatTab({ connected, enabled, setEnabled, token, setToken, baseUrl, setBaseUrl }: {
  connected: boolean;
  enabled: boolean; setEnabled: (v: boolean) => void;
  token: string; setToken: (v: string) => void;
  baseUrl: string; setBaseUrl: (v: string) => void;
}) {
  const [qrState, setQrState] = useState<"idle" | "loading" | "scanning" | "confirmed" | "error">("idle");
  const [qrUrl, setQrUrl] = useState("");
  const [qrId, setQrId] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);

  async function startQrLogin(): Promise<void> {
    setQrState("loading");
    setError(null);
    try {
      const data = await api.get<{ ok: boolean; qrcode_id: string; qrcode_url: string; error?: string }>("/api/weixin/qrcode");
      if (!data.ok) {
        setQrState("error");
        setError(data.error || "Failed to get QR code");
        return;
      }
      setQrId(data.qrcode_id);
      setQrUrl(data.qrcode_url);
      setQrState("scanning");
      // Start polling
      pollQrStatus(data.qrcode_id);
    } catch (err) {
      setQrState("error");
      setError((err as Error).message);
    }
  }

  function pollQrStatus(qrid: string): void {
    const poll = async () => {
      try {
        const data = await api.get<{ ok: boolean; status: string; has_token?: boolean; error?: string }>(`/api/weixin/status?qrcode_id=${qrid}`);
        if (data.status === "confirmed") {
          setQrState("confirmed");
          setEnabled(true);
          return;
        }
        if (data.status === "expired") {
          setQrState("idle");
          setError("QR code expired. Click Connect to generate a new one.");
          return;
        }
        if (data.status === "error") {
          setQrState("error");
          setError(data.error || "Connection error");
          return;
        }
        // Still waiting — poll again
        setTimeout(() => poll(), 1500);
      } catch {
        // Retry on network error
        setTimeout(() => poll(), 2000);
      }
    };
    poll();
  }

  return (
    <section className="max-w-xl space-y-4">
      <h2 className="text-sm uppercase tracking-wide text-muted-foreground">微信 (WeChat)</h2>
      <p className="text-xs text-muted-foreground">
        点击「连接微信」自动生成二维码，用微信扫码后自动完成登录。连接后即可在微信中与 nanobot 对话。
      </p>
      <StatusDot enabled={connected} label={connected ? "已连接" : "未连接"} />

      {qrState === "idle" && !connected && (
        <button
          type="button"
          onClick={startQrLogin}
          className="flex items-center gap-2 rounded bg-emerald-600 px-4 py-2 text-sm text-white hover:bg-emerald-700"
        >
          📱 连接微信
        </button>
      )}

      {qrState === "loading" && (
        <div className="text-sm text-muted-foreground">正在获取二维码...</div>
      )}

      {qrState === "scanning" && qrUrl && (
        <div className="rounded border border-border/40 bg-background p-4 text-center">
          <p className="mb-3 text-sm font-medium">用微信扫描下方二维码</p>
          <img
            src={`https://api.qrserver.com/v1/create-qr-code/?size=256x256&data=${encodeURIComponent(qrUrl)}`}
            alt="WeChat QR Code"
            className="mx-auto"
          />
          <p className="mt-3 text-xs text-muted-foreground">二维码有效期约 2 分钟，过期后需重新生成</p>
        </div>
      )}

      {qrState === "confirmed" && (
        <div className="rounded border border-emerald-500/30 bg-emerald-500/5 p-4 text-center text-sm text-emerald-600">
          ✅ 微信登录成功！Token 已保存。请重启 gateway 以连接微信：运行 <code>nanobot gateway restart</code>
        </div>
      )}

      {qrState === "error" && error && (
        <div className="rounded border border-destructive/30 bg-destructive/5 p-4 text-sm text-destructive">
          {error}
        </div>
      )}

      {connected && (
        <div className="mt-4 space-y-3">
          <div className="flex items-center gap-2">
            <ToggleField label="Enable WeChat" checked={enabled} onChange={setEnabled} />
          </div>
          <InputField label="Token" value={token} onChange={setToken} type="password" />
          <InputField label="Base URL" value={baseUrl} onChange={setBaseUrl} />
          <p className="text-xs text-muted-foreground">✅ 微信已连接。Token 有效，可在微信中与 nanobot 对话。</p>
          <button
            type="button"
            onClick={() => { setQrState("idle"); setQrUrl(""); setError(null); }}
            className="mt-2 flex items-center gap-1.5 rounded border border-border px-3 py-1.5 text-sm text-muted-foreground hover:bg-muted/40"
          >
            🔄 重新登录（扫码）
          </button>
        </div>
      )}
    </section>
  );
}

function ChannelCard({ title, enabled, detail }: { title: string; enabled: boolean; detail: string }) {
  return (
    <article className="rounded border border-border/40 p-4">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-medium">{title}</h3>
        <span className={cn("h-2 w-2 rounded-full", enabled ? "bg-emerald-500" : "bg-muted-foreground/40")} />
      </div>
      <p className="mt-2 text-xs text-muted-foreground">{detail}</p>
    </article>
  );
}

interface WxMpStatus {
  logged_in: boolean;
  expired: boolean;
  login_at: number | null;
  hours_since_login: number | null;
  hours_until_expire: number | null;
  ttl_hours: number;
  token: string;
  cookies_count: number;
}

function useWxMpStatus() {
  const [status, setStatus] = useState<WxMpStatus | null>(null);
  const refresh = useCallback(async () => {
    try {
      const data = await api.get<WxMpStatus>("/api/wxmp/status");
      setStatus(data);
    } catch {
      setStatus(null);
    }
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 60_000);
    return () => clearInterval(t);
  }, [refresh]);
  return { status, refresh };
}

function WxMpOverviewCard() {
  const { status } = useWxMpStatus();
  if (!status) {
    return <ChannelCard title="微信公众平台" enabled={false} detail="…" />;
  }
  if (status.logged_in && status.hours_until_expire != null) {
    const ok = status.hours_until_expire > 12;
    return (
      <ChannelCard
        title="微信公众平台"
        enabled={ok}
        detail={`已扫码 · ${status.cookies_count} cookies · ${status.hours_until_expire}h 后过期`}
      />
    );
  }
  if (status.expired) {
    return <ChannelCard title="微信公众平台" enabled={false} detail="凭证已过期 · 请重新扫码" />;
  }
  return <ChannelCard title="微信公众平台" enabled={false} detail="未扫码登录" />;
}

function WxMpTab() {
  const { status, refresh } = useWxMpStatus();
  const [busy, setBusy] = useState<"login" | "logout" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [lastResult, setLastResult] = useState<string | null>(null);

  async function startLogin(): Promise<void> {
    setBusy("login");
    setError(null);
    setLastResult(null);
    try {
      const data = await api.post<{ ok: boolean; error?: string }>("/api/wxmp/login");
      if (!data.ok) {
        setError(data.error || "登录失败");
      } else {
        setLastResult("扫码登录成功，cookies 已保存");
      }
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
      await refresh();
    }
  }

  async function doLogout(): Promise<void> {
    setBusy("logout");
    setError(null);
    try {
      await api.post("/api/wxmp/logout");
      setLastResult("已清除登录状态，下次抓取微信文章会回退");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
      await refresh();
    }
  }

  // Status pill: green (fresh), yellow (<12h), red (expired), gray (never)
  function StatusPill({ status }: { status: WxMpStatus | null }) {
    if (!status) return <StatusDot enabled={false} label="加载中..." />;
    if (status.logged_in) {
      const remaining = status.hours_until_expire ?? 0;
      const stale = remaining < 12;
      return (
        <div className="space-y-1">
          <StatusDot
            enabled={!stale}
            label={stale ? "已扫码 — 凭证即将过期" : "已扫码登录"}
          />
          <p className="text-xs text-muted-foreground">
            上次登录 {status.hours_since_login}h 前 ·{" "}
            <span className={cn(stale ? "text-amber-600" : "text-emerald-600")}>
              还有 {remaining}h 过期
            </span>
            {" "}({status.ttl_hours}h 总有效期)
          </p>
          <p className="text-xs text-muted-foreground">
            token: <code className="rounded bg-muted/40 px-1 py-0.5">{status.token}</code>
            {" · "}
            cookies: {status.cookies_count}
          </p>
        </div>
      );
    }
    if (status.expired) {
      return <StatusDot enabled={false} label="凭证已过期，需要重新扫码" />;
    }
    return <StatusDot enabled={false} label="未扫码登录" />;
  }

  return (
    <section className="max-w-xl space-y-4">
      <h2 className="text-sm uppercase tracking-wide text-muted-foreground">
        微信公众平台 (Operator Session)
      </h2>
      <p className="text-xs text-muted-foreground">
        扫码登录后，nanobot 才能拿到 mp.weixin.qq.com 文章正文（IMA OpenAPI 拿不到公众号文章 SSR 渲染的内容，本功能用本地 Edge + Playwright 模拟扫码登录）。
        凭证 96h 有效，到期前请重新扫码。
      </p>

      <StatusPill status={status} />

      {error && (
        <div className="rounded border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive">
          {error}
        </div>
      )}
      {lastResult && (
        <div className="rounded border border-emerald-500/30 bg-emerald-500/5 p-3 text-sm text-emerald-600">
          {lastResult}
        </div>
      )}

      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={startLogin}
          disabled={busy !== null}
          className="flex items-center gap-2 rounded bg-emerald-600 px-4 py-2 text-sm text-white hover:bg-emerald-700 disabled:opacity-50"
        >
          {busy === "login" ? "⏳ 等待扫码（Edge 已弹出）…" : status?.logged_in ? "🔄 重新扫码登录" : "📱 扫码登录"}
        </button>
        {status?.logged_in && (
          <button
            type="button"
            onClick={doLogout}
            disabled={busy !== null}
            className="flex items-center gap-2 rounded border border-border px-3 py-2 text-sm text-muted-foreground hover:bg-muted/40 disabled:opacity-50"
          >
            清除凭证
          </button>
        )}
      </div>

      <p className="text-xs text-muted-foreground">
        ⚠ 仅在 gateway 与显示设备同机时此按钮可用（即浏览器能在你眼前弹出来）。无显示环境下，请用 <code>nanobot channels login wxmp_platform</code> 在本机跑。
      </p>
    </section>
  );
}


