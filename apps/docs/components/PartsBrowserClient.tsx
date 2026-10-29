import { useState } from "react";

export const SLOTS = ["head", "body", "bottom", "item", "glasses"] as const;
type Slot = (typeof SLOTS)[number];

// 一覧の中身は PartsBrowser.astro がビルド時に作る。島で生成すると
// マニフェストと @humation/core (700KB 超) がブラウザに載る。
export type PartEntry = {
  id: string;
  name: string;
  slot: string;
  previewSrc: string;
};

export default function PartsBrowserClient({
  entries,
}: {
  entries: PartEntry[];
}) {
  const [query, setQuery] = useState("");
  const [slot, setSlot] = useState<Slot | "all">("all");
  const [copied, setCopied] = useState("");

  const visible = entries.filter(
    (entry) =>
      (slot === "all" || entry.slot === slot) &&
      (query === "" || entry.name.includes(query.trim().toLowerCase())),
  );

  async function copyName(entry: PartEntry) {
    try {
      await navigator.clipboard.writeText(entry.name);
      setCopied(entry.id);
      window.setTimeout(() => setCopied(""), 1200);
    } catch {
      setCopied("");
    }
  }

  return (
    <div className="not-prose">
      <div className="mt-6 flex flex-wrap items-center gap-2">
        <input
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search parts"
          spellCheck={false}
          aria-label="Search parts"
          className="border-border bg-background focus:border-muted-foreground min-w-40 flex-1 rounded-md border px-3 py-1.5 text-sm outline-none"
        />
        <div className="flex flex-wrap gap-1">
          {(["all", ...SLOTS] as const).map((item) => (
            <button
              key={item}
              type="button"
              onClick={() => setSlot(item)}
              aria-pressed={item === slot}
              className={
                item === slot
                  ? "bg-muted text-foreground rounded-md px-2.5 py-1.5 font-mono text-xs font-medium"
                  : "text-muted-foreground hover:text-foreground rounded-md px-2.5 py-1.5 font-mono text-xs transition"
              }
            >
              {item}
            </button>
          ))}
        </div>
      </div>

      <div className="text-muted-foreground mt-3 text-[13px]">
        {visible.length} of {entries.length} parts
      </div>

      <ul className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-4">
        {visible.map((entry) => (
          <li key={entry.id}>
            <button
              type="button"
              onClick={() => copyName(entry)}
              title={`Click to copy "${entry.name}"`}
              className="group border-border hover:border-muted-foreground relative w-full rounded-md border bg-(--humation-card) p-3 text-left transition"
            >
              {/* 絶対配置。名前の行に置くと幅を奪って長い名前が折り返す。 */}
              <span
                aria-hidden="true"
                className={
                  copied === entry.id
                    ? "text-accent absolute top-2 right-2"
                    : "text-muted-foreground absolute top-2 right-2 opacity-0 transition-opacity duration-150 group-hover:opacity-100"
                }
              >
                {copied === entry.id ? <CheckIcon /> : <CopyIcon />}
              </span>
              {/* 線画は背景が透明なので、暗い地では黒い部分が消える。
                  アバターのプレートと同じ扱いで、ダークのときだけ下に敷く。 */}
              <img
                src={entry.previewSrc}
                alt=""
                loading="lazy"
                data-no-zoom
                className="mx-auto h-16 w-16 rounded-sm dark:bg-[#F6F5F4]"
              />
              {/* .prose の段落余白は not-prose の中まで効くので <p> は使わない。 */}
              <div className="text-foreground mt-2 font-mono text-[11px] leading-4 break-all">
                {entry.name}
              </div>
            </button>
          </li>
        ))}
      </ul>

      {visible.length === 0 ? (
        <div className="text-muted-foreground mt-6 text-sm">
          No parts match “{query}”.
        </div>
      ) : null}
    </div>
  );
}

function CheckIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
    >
      <path d="m20 6-11 11-5-5" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg
      aria-hidden="true"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className="h-3.5 w-3.5"
    >
      <rect width="14" height="14" x="8" y="8" rx="2" />
      <path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" />
    </svg>
  );
}
