import "./globals.css";

export const metadata = {
  title: "Grid Strike Bot — Cloud",
  description:
    "Cloud dashboard for the Grid Strike trading bot (in development)",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
