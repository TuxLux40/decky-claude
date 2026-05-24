import commonjs from "@rollup/plugin-commonjs";
import resolve from "@rollup/plugin-node-resolve";
import replace from "@rollup/plugin-replace";
import typescript from "@rollup/plugin-typescript";

export default {
  input: "src/index.tsx",
  plugins: [
    commonjs(),
    resolve(),
    typescript(),
    replace({
      preventAssignment: false,
      "process.env.NODE_ENV": JSON.stringify("production"),
    }),
  ],
  external: ["react", "react-dom", "@decky/ui", "@decky/api"],
  output: {
    file: "dist/index.js",
    globals: {
      react: "SP_REACT",
      "react-dom": "SP_REACTDOM",
      "@decky/ui": "DFL",
      "@decky/api": "DFL",
    },
    format: "iife",
    exports: "default",
  },
};
