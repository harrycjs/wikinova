import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Puzzle, Trash2 } from "lucide-react";

import { api } from "@/lib/api-client";
import { cn } from "@/lib/utils";

type Kind = "channel" | "provider" | "tool";

interface PluginInfo {
  name: string;
  kind: Kind;
  version?: string;
  enabled: boolean;
  description?: string;
}

export function PluginsView(): JSX.Element {
  const { t } = useTranslation();
  const [plugins, setPlugins] = useState<PluginInfo[]>([]);
  const [busy, setBusy] = useState(false);
  const [activeKind, setActiveKind] = useState<Kind>("tool");

  async function refresh(): Promise<void> {
    setBusy(true);
    try {
      const data = await api.get<{ plugins: PluginInfo[] }>("/api/plugins/list");
      setPlugins(data.plugins);
    } catch {
      setPlugins([]);
    } finally {
      setBusy(false);
    }
  }

  async function uninstall(p: PluginInfo): Promise<void> {
    if (!confirm(t("plugins.uninstallConfirm", { name: p.name }))) return;
    setBusy(true);
    try {
      await api.post("/api/plugins/uninstall", { kind: p.kind, name: p.name });
      await refresh();
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  const filtered = plugins.filter((p) => p.kind === activeKind);

  return (
    <div className="jobs-mode flex h-full flex-col">
      <header className="flex items-center gap-3 border-b border-border/40 px-6 py-4">
        <Puzzle className="h-5 w-5 text-muted-foreground" />
        <h1 className="text-lg font-medium">{t("plugins.title")}</h1>
        <div className="ml-6 flex items-center gap-1">
          {(["tool", "channel", "provider"] as Kind[]).map((kind) => (
            <button
              key={kind}
              type="button"
              onClick={() => setActiveKind(kind)}
              className={cn(
                "rounded px-3 py-1 text-sm",
                activeKind === kind
                  ? "bg-muted/60 text-foreground"
                  : "text-muted-foreground hover:bg-muted/30",
              )}
            >
              {t(`plugins.${kind}`)}
            </button>
          ))}
        </div>
      </header>
      <div className="flex-1 overflow-y-auto p-6">
        <table className="w-full text-sm">
          <thead className="border-b border-border/40 text-xs uppercase tracking-wide text-muted-foreground">
            <tr>
              <th className="py-2 text-left font-normal">{t("plugins.name")}</th>
              <th className="py-2 text-left font-normal">{t("plugins.version")}</th>
              <th className="py-2 text-left font-normal">{t("plugins.description")}</th>
              <th className="py-2 w-12" />
            </tr>
          </thead>
          <tbody className="divide-y divide-border/40">
            {filtered.map((p) => (
              <tr key={`${p.kind}/${p.name}`}>
                <td className="py-2 font-medium">{p.name}</td>
                <td className="py-2 text-muted-foreground">{p.version || "—"}</td>
                <td className="py-2 text-muted-foreground">{p.description || "—"}</td>
                <td className="py-2 text-right">
                  <button
                    type="button"
                    onClick={() => uninstall(p)}
                    className="text-muted-foreground hover:text-destructive"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                </td>
              </tr>
            ))}
            {filtered.length === 0 && !busy && (
              <tr>
                <td colSpan={4} className="py-6 text-center text-xs text-muted-foreground">
                  {t("plugins.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}