/** @type {import('tailwindcss').Config} */
module.exports = {
  content: ["./app/templates/**/*.html", "./app/static/js/**/*.js"],
  theme: {
    extend: {
      colors: {
        brand: {
          50: "#f0f7fb",
          100: "#dbecf5",
          200: "#b9d9ea",
          300: "#8bbfda",
          400: "#559dc4",
          500: "#2f7fac",
          600: "#1f6590",
          700: "#1b5375",
          800: "#1a4661",
          900: "#0f2f43",
          950: "#091e2b",
        },
      },
      fontFamily: {
        sans: ['"Inter"', 'system-ui', '-apple-system', '"Segoe UI"', 'Roboto', '"Helvetica Neue"', 'Arial', 'sans-serif'],
        serif: ['Georgia', '"Times New Roman"', 'serif'],
        mono: ['"SF Mono"', 'ui-monospace', 'Menlo', 'Consolas', 'monospace'],
      },
      boxShadow: {
        sm: "0 1px 2px 0 rgb(15 47 67 / 0.06)",
        xl: "0 20px 40px -12px rgb(9 30 43 / 0.25)",
      },
    },
  },
  plugins: [],
};
