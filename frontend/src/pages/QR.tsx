import { useEffect, useState } from "react";
import { Navigate, useParams } from "react-router-dom";
import { kvGet } from "../offline";
import { loadPrefill, type StationInfo } from "./Shift";

/** One scan opens the right screen. Resolved from the saved copy, so it works with no signal. */
export default function QR() {
  const { token } = useParams();
  const [to, setTo] = useState<string | null>(null);
  useEffect(() => {
    void (async () => {
      const st = (await kvGet<StationInfo[]>("prefill")) ?? (await loadPrefill());
      for (const s of st) {
        const p = s.pumps.find((x) => x.qr_token === token);
        if (p) return setTo(`/shift?pump=${p.id}`);
        const t = s.tanks.find((x) => x.qr_token === token);
        if (t) return setTo(`/dip?tank=${t.id}`);
      }
      setTo("/");
    })();
  }, [token]);
  return to ? <Navigate to={to} replace /> : <p className="muted center">Opening…</p>;
}
