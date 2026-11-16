// Bundles the panel into ONE file shipped with the integration.
// Deterministic: the CI rebuilds it and fails if the committed file differs.
import * as esbuild from "esbuild";

const options = {
  entryPoints: ["src/energy-mapper-panel.ts"],
  outfile: "../custom_components/energy_mapper/frontend/energy-mapper-panel.js",
  bundle: true,
  format: "esm",
  target: "es2022",
  minify: true,
  legalComments: "eof",
  charset: "utf8",
  sourcemap: false,
  logLevel: "info",
};

if (process.argv.includes("--watch")) {
  const ctx = await esbuild.context(options);
  await ctx.watch();
} else {
  await esbuild.build(options);
}
