import type { Config } from "tailwindcss";
export default {
  content: ["./app/**/*.{ts,tsx}", "./components/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        ink: { 950: "#08090c", 900: "#0c0e13", 850: "#11141b", 800: "#171b24", 700: "#222836", 600: "#323a4d", 500: "#4a5568" },
        line: "#232936",
        mint: "#4ade80",
        amber: "#fbbf24",
        rose: "#fb7185",
        sky: "#38bdf8",
        violet: "#a78bfa",
      },
      fontFamily: { mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"] },
    },
  },
  plugins: [],
} satisfies Config;
