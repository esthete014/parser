document.addEventListener("DOMContentLoaded", () => {
    console.log("jutsu_parse loaded");
});

async function shutdownServer() {
    if (!confirm("Выключить сервер?")) return;
    try {
        const r = await fetch("/api/shutdown", { method: "POST" });
        const d = await r.json();
        if (d.ok) {
            document.body.innerHTML = `
                <div style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;gap:16px;">
                    <h1>Сервер завершает работу</h1>
                    <p style="color:var(--text-secondary);">Вы можете закрыть это окно</p>
                </div>`;
        }
    } catch (_) {
        document.body.innerHTML = `
            <div style="display:flex;justify-content:center;align-items:center;height:100vh;flex-direction:column;gap:16px;">
                <h1>Сервер остановлен</h1>
                <p style="color:var(--text-secondary);">Вы можете закрыть это окно</p>
            </div>`;
    }
}
