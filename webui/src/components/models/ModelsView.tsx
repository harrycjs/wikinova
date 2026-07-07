import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Cpu } from "lucide-react";

import { api } from "@/lib/api-client";
import { cn } from "@/lib/utils";

interface PresetInfo {
  name: string;
  label?: string;
  active?: boolean;
  model: string;
  provider: string;
  max_tokens?: number;
  context_window_tokens?: number;
}

export function ModelsView(): JSX.Element {
  const { t } = useTranslation();
  const [model, setModel] = useState("");
  const [provider, setProvider] = useState("");
  const [presets, setPresets] = useState<PresetInfo[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function refresh(): Promise<void> {
    try {
      const data = await api.get<{ agent: { model: string; provider: string }; model_presets: PresetInfo[] }>("/api/settings");
      setModel(data.agent?.model ?? "");
      setProvider(data.agent?.provider ?? "");
      setPresets(data.model_presets ?? []);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="jobs-mode flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-border/40 px-6 py-4">
        <Cpu className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-lg font-medium">{t("models.title")}</h1>
      </header>
      {error && (
        <div className="border-b border-destructive/30 bg-destructive/5 px-6 py-2 text-sm text-destructive">
          {error}
        </div>
      )}
      <div className="flex-1 overflow-y-auto p-6">
        {/* Active model */}
        <div className="mb-6 rounded border border-foreground/30 bg-background p-5">
          <h2 className="mb-1 text-sm font-medium">{t("models.active")}</h2>
          <div className="text-lg">{model || "—"}</div>
          <div className="text-xs text-muted-foreground">{provider}</div>
        </div>
        {/* Presets list */}
        <h3 className="mb-2 text-xs uppercase tracking-wide text-muted-foreground">{t("models.presets")}</h3>
        <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
          {presets.map((p) => (
            <article
              key={p.name}
              className={cn(
                "rounded border bg-background p-4",
                p.active ? "border-foreground/30" : "border-border/40",
              )}
            >
              <div className="flex items-center justify-between">
                <h3 className="text-sm font-medium">{p.label || p.name}</h3>
                {p.active && (
                  <span className="rounded-full bg-foreground px-2 py-0.5 text-xs text-background">
                    {t("models.active")}
                  </span>
                )}
              </div>
              <div className="mt-1 text-xs text-muted-foreground">{p.model}</div>
              <div className="mt-1 text-xs text-muted-foreground/60">{p.provider}</div>
            </article>
          ))}
          {presets.length === 0 && (
            <div className="col-span-full text-sm text-muted-foreground">{t("models.empty")}</div>
          )}
        </div>
      </div>
    </div>
  );
}
