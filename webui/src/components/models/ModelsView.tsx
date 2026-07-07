import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Cpu, Save, Key, Check } from "lucide-react";

import { api } from "@/lib/api-client";
import { cn } from "@/lib/utils";

// All supported providers with their default models
const PROVIDERS = [
  { id: "deepseek", label: "DeepSeek (国内)", defaultModel: "deepseek-v4-flash", needsKey: true, website: "platform.deepseek.com" },
  { id: "openai", label: "OpenAI", defaultModel: "gpt-4o", needsKey: true, website: "platform.openai.com" },
  { id: "anthropic", label: "Anthropic", defaultModel: "claude-opus-4-5", needsKey: true, website: "console.anthropic.com" },
  { id: "openrouter", label: "OpenRouter (多模型聚合)", defaultModel: "anthropic/claude-opus-4-5", needsKey: true, website: "openrouter.ai" },
  { id: "gemini", label: "Google Gemini", defaultModel: "gemini-2.5-flash", needsKey: true, website: "aistudio.google.com" },
  { id: "moonshot", label: "Moonshot (月之暗面)", defaultModel: "moonshot-v1-auto", needsKey: true, website: "platform.moonshot.cn" },
  { id: "dashscope", label: "阿里百炼 (DashScope)", defaultModel: "qwen-plus", needsKey: true, website: "dashscope.aliyun.com" },
  { id: "zhipu", label: "智谱 (ZhipuAI)", defaultModel: "glm-4-flash", needsKey: true, website: "open.bigmodel.cn" },
  { id: "minimax", label: "MiniMax", defaultModel: "MiniMax-M3", needsKey: true, website: "platform.minimaxi.com" },
  { id: "stepfun", label: "阶跃星辰 (StepFun)", defaultModel: "step-2-16k", needsKey: true, website: "platform.stepfun.com" },
  { id: "volcengine", label: "火山引擎 (VolcEngine)", defaultModel: "doubao-pro-32k", needsKey: true, website: "console.volcengine.com" },
  { id: "siliconflow", label: "硅基流动 (SiliconFlow)", defaultModel: "deepseek-ai/DeepSeek-V3", needsKey: true, website: "siliconflow.cn" },
  { id: "xiaomi_mimo", label: "小米 MiMo", defaultModel: "MiMo-7B", needsKey: true, website: "dev.mi.com" },
  { id: "azure_openai", label: "Azure OpenAI", defaultModel: "gpt-4o", needsKey: true, website: "azure.openai.com" },
  { id: "aws_bedrock", label: "AWS Bedrock", defaultModel: "anthropic.claude-3-5-sonnet-20241022-v2:0", needsKey: true, website: "aws.amazon.com/bedrock" },
  { id: "ollama", label: "Ollama (本地)", defaultModel: "llama3.1", needsKey: false, website: "ollama.com" },
  { id: "lmstudio", label: "LM Studio (本地)", defaultModel: "local-model", needsKey: false, website: "lmstudio.ai" },
  { id: "vllm", label: "vLLM (本地)", defaultModel: "local-model", needsKey: false, website: "docs.vllm.ai" },
  { id: "custom", label: "自定义 (OpenAI 兼容)", defaultModel: "", needsKey: true, website: "" },
];

interface ConfiguredModel {
  provider: string;
  providerLabel: string;
  model: string;
  isActive: boolean;
}

export function ModelsView(): JSX.Element {
  const { t } = useTranslation();
  const [activeModel, setActiveModel] = useState("");
  const [activeProvider, setActiveProvider] = useState("");
  const [configuredModels, setConfiguredModels] = useState<ConfiguredModel[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  // Load current config
  async function refresh(): Promise<void> {
    try {
      const data = await api.get<{ agent: { model: string; provider: string }; providers: Record<string, Record<string, unknown>> }>("/api/settings");
      const agent = data.agent || {};
      setActiveProvider(agent.provider || "");
      setActiveModel(agent.model || "");

      // Build configured models list
      const models: ConfiguredModel[] = [];
      for (const [key, cfg] of Object.entries(data.providers || {})) {
        const provider = PROVIDERS.find((p) => p.id === key);
        if (provider && (cfg as Record<string, unknown>).apiKey) {
          models.push({
            provider: key,
            providerLabel: provider.label,
            model: agent.provider === key ? (agent.model || provider.defaultModel) : provider.defaultModel,
            isActive: agent.provider === key,
          });
        }
      }
      setConfiguredModels(models);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  // Switch to a configured model
  async function enableModel(provider: string): Promise<void> {
    const cfg = configuredModels.find((m) => m.provider === provider);
    if (!cfg) return;
    setError(null);
    try {
      await api.get(`/api/settings/update?model=${encodeURIComponent(cfg.model)}&provider=${encodeURIComponent(provider)}`);
      setSaved(`Switched to ${cfg.providerLabel}`);
      await refresh();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="jobs-mode flex h-full flex-col">
      <header className="flex items-center justify-between border-b border-border/40 px-6 py-4">
        <div className="flex items-center gap-3">
          <Cpu className="h-5 w-5 text-muted-foreground" />
          <h1 className="text-lg font-medium">模型配置</h1>
        </div>
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

      <div className="flex-1 overflow-y-auto p-6 space-y-6">
        {/* Current Active Model */}
        <section>
          <h3 className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">当前激活</h3>
          <div className="rounded border border-emerald-500/30 bg-emerald-500/5 p-5">
            <div className="flex items-center gap-3">
              <span className="h-3 w-3 rounded-full bg-emerald-500" />
              <div>
                <div className="text-lg font-medium">{activeModel || "未配置"}</div>
                <div className="text-sm text-muted-foreground">{activeProvider || "—"}</div>
              </div>
            </div>
          </div>
        </section>

        {/* Configured Models - Cards with Enable button */}
        <section>
          <h3 className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">已配置模型</h3>
          {configuredModels.length === 0 ? (
            <div className="rounded border border-border/40 bg-background p-8 text-center text-sm text-muted-foreground">
              暂无已配置模型。请在下方添加新的模型提供商。
            </div>
          ) : (
            <div className="grid gap-2 md:grid-cols-2 lg:grid-cols-3">
              {configuredModels.map((m) => (
                <div
                  key={m.provider}
                  className={cn(
                    "rounded border p-4 transition-all",
                    m.isActive
                      ? "border-emerald-500/40 bg-emerald-500/5"
                      : "border-border/40 bg-background hover:border-border",
                  )}
                >
                  <div className="flex items-center justify-between mb-2">
                    <span className="text-sm font-medium">{m.providerLabel}</span>
                    {m.isActive && (
                      <span className="rounded-full bg-emerald-500 px-2 py-0.5 text-[10px] text-white">Active</span>
                    )}
                  </div>
                  <div className="text-xs text-muted-foreground mb-3 truncate">{m.model}</div>
                  {!m.isActive && (
                    <button
                      type="button"
                      onClick={() => enableModel(m.provider)}
                      className="w-full rounded border border-border/60 px-3 py-1.5 text-xs hover:bg-muted/30"
                    >
                      <Check className="inline h-3 w-3 mr-1" />
                      启用
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </section>

        {/* Add New Provider */}
        <section>
          <h3 className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">添加新提供商</h3>
          <NewProviderForm onAdded={refresh} />
        </section>
      </div>
    </div>
  );
}

function NewProviderForm({ onAdded }: { onAdded: () => void }) {
  const [provider, setProvider] = useState("deepseek");
  const [model, setModel] = useState("deepseek-v4-flash");
  const [apiKey, setApiKey] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);

  const currentProvider = PROVIDERS.find((p) => p.id === provider) || PROVIDERS[0];

  async function handleSave() {
    setError(null);
    setSaved(null);
    try {
      // Save API key if provided
      if (apiKey && currentProvider.needsKey) {
        await api.get(`/api/settings/provider/update?provider=${provider}&apiKey=${encodeURIComponent(apiKey)}`);
      }
      // Apply the model
      await api.get(`/api/settings/update?model=${encodeURIComponent(model)}&provider=${encodeURIComponent(provider)}`);
      setSaved(`Added ${currentProvider.label} with model ${model}`);
      setApiKey("");
      onAdded();
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="rounded border border-border/40 bg-background p-4 space-y-4">
      {error && <div className="text-sm text-destructive">{error}</div>}
      {saved && <div className="text-sm text-emerald-600">{saved}</div>}

      <div>
        <label className="mb-1 block text-xs text-muted-foreground">选择提供商</label>
        <select
          value={provider}
          onChange={(e) => {
            const p = PROVIDERS.find((x) => x.id === e.target.value);
            if (p) {
              setProvider(p.id);
              setModel(p.defaultModel);
            }
          }}
          className="w-full rounded border border-border/60 bg-background px-3 py-2 text-sm outline-none focus:border-foreground/40"
        >
          {PROVIDERS.map((p) => (
            <option key={p.id} value={p.id}>{p.label}</option>
          ))}
        </select>
      </div>

      <div>
        <label className="mb-1 block text-xs text-muted-foreground">模型名称</label>
        <input
          type="text"
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder={currentProvider.defaultModel}
          className="w-full rounded border border-border/60 bg-background px-3 py-2 text-sm outline-none focus:border-foreground/40"
        />
        <p className="mt-1 text-xs text-muted-foreground">默认: {currentProvider.defaultModel}</p>
      </div>

      {currentProvider.needsKey && (
        <div>
          <label className="mb-1 block text-xs text-muted-foreground">API Key</label>
          <div className="flex gap-2">
            <input
              type="password"
              value={apiKey}
              onChange={(e) => setApiKey(e.target.value)}
              placeholder={`Enter ${currentProvider.label} API key`}
              className="flex-1 rounded border border-border/60 bg-background px-3 py-2 text-sm outline-none focus:border-foreground/40"
            />
          </div>
          {currentProvider.website && (
            <p className="mt-1 text-xs">
              <a href={`https://${currentProvider.website}`} target="_blank" rel="noreferrer" className="text-blue-500 hover:underline">
                获取 API Key →
              </a>
            </p>
          )}
        </div>
      )}

      <div className="flex justify-end">
        <button
          type="button"
          onClick={handleSave}
          className="flex items-center gap-1.5 rounded bg-foreground px-4 py-2 text-sm text-background hover:bg-foreground/90"
        >
          <Save className="h-3.5 w-3.5" />
          添加并启用
        </button>
      </div>
    </div>
  );
}
