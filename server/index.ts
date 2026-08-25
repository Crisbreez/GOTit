import "dotenv/config";
import express, { Response, NextFunction } from 'express';
import type { Request } from 'express';
import { registerRoutes } from "./routes";
import { serveStatic } from "./static";
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import path from "node:path";

// ── Daily matchup fit pull ───────────────────────────────────────────────
// Runs python/run_matchup_fit.py once per day at ~8:30am ET: pulls platoon
// splits (vs-L/vs-R) for every batter with active props vs tonight's
// probable pitcher, writes matchup_fit_scores rows, and stamps
// matchup_fit_score onto props. This is NOT a props refresh — props are
// still only pulled via the manual pull button.
let lastFitPullDate = "";
function runMatchupFitPull(reason: string): void {
  const scriptPath = path.resolve(process.cwd(), "python", "run_matchup_fit.py");
  const python = process.env.PYTHON_BIN || "python3";
  console.log(`[matchup_fit] daily pull starting (${reason})`);
  const child = spawn(python, [scriptPath], { cwd: process.cwd(), timeout: 300_000 });
  let out = "";
  let err = "";
  child.stdout.on("data", (d: Buffer) => { out += d.toString(); });
  child.stderr.on("data", (d: Buffer) => { err += d.toString(); });
  child.on("close", (code: number | null) => {
    if (code === 0) {
      console.log(`[matchup_fit] daily pull done: ${out.trim().slice(0, 200)}`);
    } else {
      console.error(`[matchup_fit] daily pull exited ${code}: ${err.slice(0, 300)}`);
    }
  });
  child.on("error", (e: any) => console.error("[matchup_fit] spawn error:", e.message));
}

function scheduleMatchupFitPull(): void {
  // Check every 5 minutes; fire once per day inside the 8:00-9:00am ET window.
  setInterval(() => {
    const now = new Date();
    const et = new Date(now.toLocaleString("en-US", { timeZone: "America/New_York" }));
    const dateKey = `${et.getFullYear()}-${et.getMonth() + 1}-${et.getDate()}`;
    if (et.getHours() === 8 && et.getMinutes() >= 30 && lastFitPullDate !== dateKey) {
      lastFitPullDate = dateKey;
      runMatchupFitPull("scheduled 8:30am ET");
    }
  }, 5 * 60 * 1000);
}

// Prevent EPIPE / broken-pipe socket errors from crashing the server.
// These occur when Render's egress drops a connection mid-stream.
process.on('uncaughtException', (err: any) => {
  if (err.code === 'EPIPE' || err.code === 'ECONNRESET' || err.code === 'ECONNABORTED') {
    console.warn('[server] Swallowed network error:', err.code, err.message);
    return;
  }
  console.error('[server] Uncaught exception:', err);
  process.exit(1);
});
process.on('unhandledRejection', (reason: any) => {
  console.error('[server] Unhandled rejection:', reason);
});

const app = express();
const httpServer = createServer(app);

declare module "http" {
  interface IncomingMessage {
    rawBody: unknown;
  }
}

app.use(
  express.json({
    limit: '10mb',
    verify: (req, _res, buf) => {
      req.rawBody = buf;
    },
  }),
);
app.use(express.urlencoded({ extended: true, limit: '10mb' }));

app.use(express.urlencoded({ extended: false }));

export function log(message: string, source = "express") {
  const formattedTime = new Date().toLocaleTimeString("en-US", {
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });

  console.log(`${formattedTime} [${source}] ${message}`);
}

app.use((req, res, next) => {
  const start = Date.now();
  const path = req.path;
  let capturedJsonResponse: Record<string, any> | undefined = undefined;

  const originalResJson = res.json;
  res.json = function (bodyJson, ...args) {
    capturedJsonResponse = bodyJson;
    return originalResJson.apply(res, [bodyJson, ...args]);
  };

  res.on("finish", () => {
    const duration = Date.now() - start;
    if (path.startsWith("/api")) {
      let logLine = `${req.method} ${path} ${res.statusCode} in ${duration}ms`;
      if (capturedJsonResponse) {
        logLine += ` :: ${JSON.stringify(capturedJsonResponse)}`;
      }

      log(logLine);
    }
  });

  next();
});

(async () => {
  await registerRoutes(httpServer, app);

  app.use((err: any, _req: Request, res: Response, next: NextFunction) => {
    const status = err.status || err.statusCode || 500;
    const message = err.message || "Internal Server Error";

    console.error("Internal Server Error:", err);

    if (res.headersSent) {
      return next(err);
    }

    return res.status(status).json({ message });
  });

  // importantly only setup vite in development and after
  // setting up all the other routes so the catch-all route
  // doesn't interfere with the other routes
  if (process.env.NODE_ENV === "production") {
    serveStatic(app);
  } else {
    const { setupVite } = await import("./vite");
    await setupVite(httpServer, app);
  }

  // ALWAYS serve the app on the port specified in the environment variable PORT
  // Other ports are firewalled. Default to 5000 if not specified.
  // this serves both the API and the client.
  // It is the only port that is not firewalled.
  const port = parseInt(process.env.PORT || "5000", 10);
  httpServer.listen(
    {
      port,
      host: "0.0.0.0",
      reusePort: true,
    },
    () => {
      log(`serving on port ${port}`);
      scheduleMatchupFitPull();
    },
  );
})();
