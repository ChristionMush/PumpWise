export type Role = "platform_admin" | "owner" | "manager" | "attendant";
export interface User { id: string; role: Role; full_name: string; email: string | null; phone: string | null; must_change_password: boolean; tenant_id: string | null; }
export interface TenantInfo { id: string; name: string; slug: string; status: "active" | "suspended" | "cancelled"; }
export interface Subscription { status: "active" | "past_due" | "suspended" | "cancelled"; plan: string; paid_through: string | null; }
export interface Owner { id: string; full_name: string; email: string | null; phone: string | null; }
export interface Station { id: string; name: string; address: string; status: string; }
export interface TenantSummary {
  id: string; name: string; slug: string; status: TenantInfo["status"]; created_at: string;
  subscription: Subscription | null; station_count: number; owner: Owner | null; stations?: Station[];
}
export interface Credentials { login: string; temporary_password: string; }
export interface Page<T> { count: number; next: string | null; results: T[]; }
