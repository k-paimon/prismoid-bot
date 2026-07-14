import { redirect } from "next/navigation";

// middleware already guarantees a session here; the root just forwards
export default function Home() {
  redirect("/dashboard");
}
