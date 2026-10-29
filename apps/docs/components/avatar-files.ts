import { createHash } from "node:crypto";

// プレビューの SVG は 1 枚で 30〜100KB ある。data URI で HTML に埋めると
// Parts は 1MB を超え、クライアントルーターの遷移がその取得とパースを待つ。
// ビルドでは別ファイルに出して URL だけを HTML に載せ、画像は遅延で読ませる。
//
// ページの描画とビルド完了フックは同じプロセスで走るので、globalThis に
// 集めておき、blume.config.ts の統合が dist/avatars/ に書き出す。
export const AVATAR_DIR = "avatars";
const REGISTRY_KEY = "__humationAvatarFiles";

type Registry = Map<string, string>;

export function avatarRegistry(): Registry {
  const store = globalThis as typeof globalThis & {
    [REGISTRY_KEY]?: Registry;
  };
  store[REGISTRY_KEY] ??= new Map();
  return store[REGISTRY_KEY];
}

/** svg を配信する URL を返す。dev は書き出し先がないので data URI のまま。 */
export function avatarSrc(svg: string, dataUri: string): string {
  if (import.meta.env.DEV) return dataUri;
  // 内容ハッシュのファイル名なので、同じ絵は 1 ファイルにまとまり長期キャッシュできる。
  const name = `${createHash("sha256").update(svg).digest("hex").slice(0, 16)}.svg`;
  avatarRegistry().set(name, svg);
  return `/${AVATAR_DIR}/${name}`;
}
