import { writeFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { createAvatar } from "../../../packages/core/dist/index.js";
import { humation1 } from "../../../packages/assets-humation-1/dist/index.js";

const samplesDir = dirname(fileURLToPath(import.meta.url));

const specs = [
  {
    name: "simple.svg",
    selections: {
      head: "short",
      body: "tee",
      bottom: "wide-pants",
      item: "none",
    },
    colors: {
      hair: "#2a1a10",
      clothes: "#ffffff",
      bottom: "#2f4a8a",
      skin: "#f6d7b8",
      stroke: "#111111",
    },
  },
  {
    name: "standard.svg",
    selections: {
      head: "wavy-long",
      body: "hoodie",
      bottom: "flared-skirt",
      item: "flower",
    },
    colors: {
      hair: "#6b3b1f",
      clothes: "#d9483b",
      bottom: "#2f4a8a",
      skin: "#f6d7b8",
      stroke: "#111111",
    },
  },
  {
    name: "complex.svg",
    selections: {
      head: "low-twin-buns",
      body: "jacket",
      bottom: "culottes",
      item: "calico-cat",
    },
    colors: {
      hair: "#1c1c1c",
      clothes: "#3a6e4a",
      bottom: "#c9b48a",
      skin: "#f2c9a5",
      stroke: "#111111",
    },
  },
] as const;

for (const spec of specs) {
  const svg = createAvatar(humation1, {
    selections: spec.selections,
    colors: spec.colors,
    background: "transparent",
    crop: "avatar",
  }).toString();
  const outPath = join(samplesDir, spec.name);
  await writeFile(outPath, svg);
  console.log(`Wrote ${outPath}`);
}
