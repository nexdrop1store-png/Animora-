import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({ subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Animora — AI-Native 3D Creation",
  description: "Create stunning 3D content with the power of AI. Animora brings Claude directly into your 3D workflow.",
  metadataBase: new URL("https://animora.tech"),
  openGraph: {
    title: "Animora — AI-Native 3D Creation",
    description: "Create stunning 3D content with the power of AI.",
    url: "https://animora.tech",
    siteName: "Animora",
    type: "website",
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className="dark">
      <body className={`${inter.className} bg-black text-white antialiased`}>
        {children}
      </body>
    </html>
  );
}
