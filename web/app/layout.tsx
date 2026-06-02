import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Titans of Investing — Directory",
  description: "A searchable directory of Titans of Investing alumni, built from the public class directory.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        {children}
        <footer className="site-footer">
          <div className="wrap">
            Built from the public Titans of Investing class directory. Read-only
            view — source of record is the published directory.
          </div>
        </footer>
      </body>
    </html>
  );
}
