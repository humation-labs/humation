import { mkdir, writeFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

import { defineConfig } from "blume";
import type { AstroIntegration } from "astro";

import { AVATAR_DIR, avatarRegistry } from "./components/avatar-files";

// Blume enables prefetching but defaults to hover, which starts too late for
// quick sidebar clicks. Astro can prefetch visible links before interaction.
const prefetchVisibleLinks: AstroIntegration = {
  name: "humation-prefetch-visible-links",
  hooks: {
    "astro:config:setup"({ updateConfig }) {
      updateConfig({
        prefetch: { prefetchAll: true, defaultStrategy: "viewport" },
      });
    },
  },
};

// ページの描画中に集めたプレビュー SVG を dist/avatars/ に書き出す。
// 集め方と理由は components/avatar-files.ts を参照。
const writeAvatarFiles: AstroIntegration = {
  name: "humation-avatar-files",
  hooks: {
    async "astro:build:done"({ dir, logger }) {
      const files = avatarRegistry();
      const outDir = fileURLToPath(new URL(`${AVATAR_DIR}/`, dir));
      await mkdir(outDir, { recursive: true });
      await Promise.all(
        [...files].map(([name, svg]) => writeFile(`${outDir}${name}`, svg)),
      );
      logger.info(`Wrote ${files.size} avatar previews to /${AVATAR_DIR}/`);
    },
  },
};

export default defineConfig({
  title: "Humation Docs",
  description:
    "Deterministic hand-drawn avatars from local SVG assets. No API call required.",
  // 既定の docs/ から content/ に変更するため、コンテンツルートを明示する。
  content: { root: "content" },
  integrations: [prefetchVisibleLinks, writeAvatarFiles],
  // ページ末尾の「このページは役に立ちましたか？」は集計先を持たないので出さない。
  feedback: false,
  logo: {
    image: "/icon_humation.png",
  },
  github: { owner: "humation-labs", repo: "humation", dir: "apps/docs" },
  theme: {
    accent: { light: "#008f8c", dark: "oklch(0.78 0.1 192)" },
    action: "#ff4d2e",
    background: { light: "#fdfdfc", dark: "#171717" },
  },
  deployment: { site: "https://docs.humation.app" },
  seo: {
    og: {
      logo: "/logo_humation_og.svg",
      fonts: ["Inter", "Noto Sans JP"],
    },
  },
  i18n: {
    defaultLocale: "en",
    locales: [
      { code: "en", label: "English" },
      { code: "ja", label: "日本語", style: "技術文書、丁寧語 (です/ます)" },
    ],
  },
  navigation: {
    repo: true,
    sidebar: [
      {
        label: "Guides",
        items: ["/", "/react", "/core", "/swift", "/customizing", "/state"],
      },
      {
        label: "Reference",
        items: ["/parts", "/options", "/llms", "/changelog"],
      },
    ],
  },
});
