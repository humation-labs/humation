import { defineConfig } from "blume";

export default defineConfig({
  title: "Humation Docs",
  description:
    "Deterministic hand-drawn avatars from local SVG assets. No API call required.",
  // 既定の docs/ から content/ に変更するため、コンテンツルートを明示する。
  content: { root: "content" },
  logo: {
    image: "/icon_humation.png",
  },
  // 「Edit on GitHub」のリンク元。OSS リポの docs/ は内部設計文書が使っているので、
  // ドキュメント本体の置き場所である apps/docs/ を指す。
  github: { owner: "humation-labs", repo: "humation", dir: "apps/docs" },
  theme: {
    // config に置いた accent / action / background は Blume が
    // :root[data-theme="dark"] にも書き出す (src/theme/palette.ts)。
    // teal はそのままだとダーク背景で沈むので明度だけ持ち上げる。
    accent: { light: "#008f8c", dark: "oklch(0.78 0.1 192)" },
    action: "#ff4d2e",
    // ライトが純白を外したオフホワイトなので、ダークも純黒から外す。
    // ダークは Tailwind neutral の段に乗せた純グレー (zinc の青紫寄りは使わない)。
    // これに合わせた中性色一式は theme.css のダークブロックで持つ。
    background: { light: "#fdfdfc", dark: "#171717" },
  },
  // Blume が site URL を自動検出するのは Vercel / Netlify / Cloudflare Pages。
  // wrangler で Workers に直接出す構成では環境変数が来ないので明示する。
  // 未設定だと sitemap.xml / robots.txt / llms.txt が黙って生成されない。
  deployment: { site: "https://docs.humation.app" },
  seo: {
    og: {
      // カードは上の logo.image を見ず、SVG を 1 枚だけ受け取る (未設定だと "H" タイル)。
      // ヘッダーと同じアイコン + ワードマークを 1 枚に合成したものがこれ。
      logo: "/logo_humation_og.svg",
      // カードの組み込みフォントは Latin のみで、ja のタイトルが豆腐になる。
      // theme.css はシステムフォント指定なのでカード側から引けず、ここで名指しする。
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
