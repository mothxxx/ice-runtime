async function fetchRequests() {
  try {
    const res = await fetch("/daemon/pairing/requests");
    if (!res.ok) return;
    const data = await res.json();
    const el = document.getElementById("daemon-content");
    const reqs = data.requests || [];
    if (!reqs.length) {
      el.innerHTML =
        '<p class="daemon-empty">No pending pairing requests.</p>';
      return;
    }
    const r = reqs[0];

    el.innerHTML = `
      <div class="daemon-request-card">
        <div class="daemon-label">Incoming flake</div>
        <div class="daemon-value"><strong>Client</strong> ${
          r.client_ip || "unknown"
        }</div>
        <div class="daemon-value"><strong>Request ID</strong> ${
          r.request_id
        }</div>
        <div class="daemon-value">
          <span class="daemon-status-pill">
            <span>❄</span>
            <span>${r.status.toUpperCase()}</span>
          </span>
        </div>
        <div class="daemon-actions">
          <button class="daemon-btn daemon-btn-ghost" onclick="dismiss('${
            r.request_id
          }')">Ignore</button>
          <button class="daemon-btn daemon-btn-primary" onclick="approve('${
            r.request_id
          }')">Accept flake</button>
        </div>
        <div id="daemon-status-row" class="daemon-success"></div>
      </div>
    `;
  } catch (err) {
    console.error("Failed to fetch pairing requests", err);
  }
}

async function approve(id) {
  try {
    const res = await fetch("/daemon/pairing/approve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: id }),
    });
    const data = await res.json();
    const row = document.getElementById("daemon-status-row");
    if (data.ok && data.status === "approved") {
      row.textContent =
        "❄ Flake added – this host is now trusted by ICE Studio.";
      setTimeout(() => {
        window.close();
      }, 1500);
    } else {
      row.textContent = "Failed to approve flake.";
    }
  } catch (err) {
    console.error("Failed to approve pairing", err);
  }
}

async function dismiss(id) {
  try {
    await fetch("/daemon/pairing/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ request_id: id }),
    });
    fetchRequests();
  } catch (err) {
    console.error("Failed to dismiss pairing request", err);
  }
}

fetchRequests();
setInterval(fetchRequests, 4000);
