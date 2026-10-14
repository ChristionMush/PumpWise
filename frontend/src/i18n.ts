// Translation structure. English is complete; Shona (sn) and Ndebele (nd) are intentionally
// empty until a native-speaker translator fills them in. Missing keys fall back to English.
export type Locale = "en" | "sn" | "nd";

const en = {
  appName: "PumpWise",
  builtBy: "Built by Tendeka & Elichi",
  tagline: "Know your numbers on your phone.",
  "login.title": "Sign in",
  "login.identifier": "Phone number or email",
  "login.password": "Password",
  "login.submit": "Sign in",
  "login.wrong": "Wrong login details.",
  "common.loading": "Loading…",
  "common.save": "Save",
  "common.cancel": "Cancel",
  "common.signOut": "Sign out",
  "common.copy": "Copy",
  "common.copied": "Copied",
  "pw.title": "Choose your own password",
  "pw.intro": "You are using a temporary password. Choose a new one to continue.",
  "pw.current": "Temporary password",
  "pw.new": "New password",
  "pw.submit": "Save new password",
  "platform.title": "Businesses",
  "platform.new": "+ New business",
  "platform.search": "Search by name",
  "platform.empty": "No businesses yet.",
  "platform.more": "Show more",
  "lock.title": "Account suspended",
  "lock.body": "Access is paused because the subscription is not up to date. Contact ELICHIE to reactivate. Your records are safe.",
  "home.welcome": "Welcome",
  "home.soon": "Your dashboard is coming soon.",
} as const;

export type Key = keyof typeof en;
const dictionaries: Record<Locale, Partial<Record<Key, string>>> = { en, sn: {}, nd: {} };
let locale: Locale = (localStorage.getItem("fs_locale") as Locale) || "en";

export function setLocale(l: Locale) { locale = l; localStorage.setItem("fs_locale", l); }
export function t(key: Key): string { return dictionaries[locale][key] ?? en[key]; }
