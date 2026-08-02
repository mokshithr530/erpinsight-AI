import ChatWidget from "./components/ChatWidget";
import "./index.css";

function App() {
  return (
    <>
      <div className="min-h-screen bg-gray-100 text-gray-950">
        <header className="border-b border-gray-200 bg-white">
          <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-4">
            <div>
              <div className="text-sm font-semibold uppercase tracking-[0.18em] text-gray-500">
                SQL Assistant
              </div>
              <h1 className="mt-1 text-2xl font-semibold">
                ERP analytics assistant
              </h1>
            </div>

            <div className="hidden rounded-lg border border-emerald-200 bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-700 sm:block">
              Backend ready
            </div>
          </div>
        </header>

        <main className="mx-auto grid max-w-6xl gap-4 px-6 py-6 md:grid-cols-[1.4fr_0.8fr]">
          <section className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="text-lg font-semibold">
              Demo dashboard
            </h2>
            <p className="mt-2 max-w-2xl text-sm leading-6 text-gray-600">
              Connect the ERP database, then ask verified management questions
              across projects, billing, purchase orders, vendors, store movement
              and inventory.
            </p>

            <div className="mt-5 grid gap-3 sm:grid-cols-3">
              {["Connect ERP", "Ask question", "Review answer"].map(
                (item, index) => (
                  <div
                    key={item}
                    className="rounded-lg border border-gray-200 bg-gray-50 p-4"
                  >
                    <div className="text-xs font-semibold text-gray-500">
                      Step {index + 1}
                    </div>
                    <div className="mt-1 text-sm font-medium">
                      {item}
                    </div>
                  </div>
                )
              )}
            </div>

            <div className="mt-5">
              <div className="text-xs font-semibold uppercase tracking-[0.14em] text-gray-500">
                Verified modules
              </div>
              <div className="mt-3 flex flex-wrap gap-2">
                {[
                  "Projects",
                  "Billing",
                  "Purchase Orders",
                  "Vendors",
                  "Store Movement",
                  "Inventory",
                ].map((module) => (
                  <span
                    key={module}
                    className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1.5 text-xs font-medium text-emerald-700"
                  >
                    {module}
                  </span>
                ))}
              </div>
            </div>
          </section>

          <aside className="rounded-lg border border-gray-200 bg-white p-5">
            <h2 className="text-sm font-semibold uppercase tracking-[0.14em] text-gray-500">
              Good demo questions
            </h2>
            <div className="mt-4 flex flex-wrap gap-2">
              {[
                "List projects",
                "Top vendors by PO value",
                "Compare billing and PO by project",
                "Show material issued",
                "Show top materials issued",
              ].map((type) => (
                <span
                  key={type}
                  className="rounded-lg border border-gray-200 bg-gray-50 px-3 py-2 text-sm font-medium"
                >
                  {type}
                </span>
              ))}
            </div>

            <p className="mt-5 text-sm leading-6 text-gray-600">
              The answer uses verified ERP mappings first. Unsupported KPIs
              like profit/loss ask for clarification instead of guessing.
            </p>
          </aside>
        </main>
      </div>

      <ChatWidget />
    </>
  );
}

export default App;
