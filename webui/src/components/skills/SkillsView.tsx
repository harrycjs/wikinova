import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Sparkles } from "lucide-react";

import { api } from "@/lib/api-client";
import { cn } from "@/lib/utils";

interface Skill {
  name: string;
  description: string;
  emoji?: string;
  always?: boolean;
  enabled?: boolean;
  path?: string;
}

export function SkillsView(): JSX.Element {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<Skill[]>([]);
  const [selected, setSelected] = useState<Skill | null>(null);
  const [busy, setBusy] = useState(false);

  async function refresh(): Promise<void> {
    setBusy(true);
    try {
      const data = await api.get<{ skills: Skill[] }>("/api/webui/skills");
      setSkills(data.skills);
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    refresh();
  }, []);

  return (
    <div className="jobs-mode grid h-full grid-cols-[320px_minmax(0,1fr)] divide-x divide-border/40">
      <aside className="overflow-y-auto">
        <header className="flex items-center gap-2 border-b border-border/40 px-4 py-3">
          <Sparkles className="h-4 w-4 text-muted-foreground" />
          <h1 className="text-sm font-medium">{t("skills.title")}</h1>
        </header>
        <ul>
          {skills.map((skill) => (
            <li key={skill.name}>
              <button
                type="button"
                onClick={() => setSelected(skill)}
                className={cn(
                  "block w-full px-4 py-2 text-left text-sm",
                  selected?.name === skill.name
                    ? "bg-muted/60"
                    : "hover:bg-muted/30 text-muted-foreground",
                )}
              >
                <div className="flex items-center gap-2">
                  {skill.emoji && <span>{skill.emoji}</span>}
                  <span className="font-medium text-foreground">{skill.name}</span>
                  {skill.always && (
                    <span className="rounded-full border border-border/60 px-1.5 py-0 text-[10px] uppercase tracking-wide text-muted-foreground">
                      always
                    </span>
                  )}
                </div>
                <div className="mt-0.5 line-clamp-2 text-xs text-muted-foreground/80">
                  {skill.description}
                </div>
              </button>
            </li>
          ))}
          {skills.length === 0 && !busy && (
            <li className="px-4 py-6 text-xs text-muted-foreground">{t("skills.empty")}</li>
          )}
        </ul>
      </aside>
      <main className="overflow-y-auto px-8 py-6">
        {selected ? (
          <article>
            <h2 className="text-xl font-medium">{selected.name}</h2>
            <p className="mt-2 text-sm text-muted-foreground">{selected.description}</p>
            {selected.path && (
              <p className="mt-3 font-mono text-xs text-muted-foreground/80">
                {selected.path}
              </p>
            )}
          </article>
        ) : (
          <div className="flex h-full items-center justify-center text-sm text-muted-foreground">
            {t("skills.selectPrompt")}
          </div>
        )}
      </main>
    </div>
  );
}