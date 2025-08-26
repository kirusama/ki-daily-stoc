async function fetchData() {
    const res = await fetch("/data");
    const data = await res.json();
  
    const container = document.getElementById("watchlists");
    container.innerHTML = "";
  
    for (const [sheetName, stocks] of Object.entries(data)) {
      const tab = document.createElement("div");
      tab.innerHTML = `
        <h2>${sheetName}</h2>
        <button onclick="refreshSheet('${sheetName}')">Refresh ${sheetName}</button>
        <table border="1" cellpadding="5" style="margin: 10px 0;">
          <tr>
            <th>Scrip Name</th>
            <th>Target Price (₹)</th>
            <th>Current Price (₹)</th>
            <th>Status</th>
          </tr>
          ${stocks.map(stock => `
            <tr style="background-color: ${stock.status === '🎯 Target Hit!' ? '#90EE90' : 'white'};">
              <td>${stock['Scrip Name']}</td>
              <td>₹${stock['Target Price'].toFixed(2)}</td>
              <td>${stock['Current Price'] > 0 ? '₹' + stock['Current Price'].toFixed(2) : 'N/A'}</td>
              <td>${stock['Status']}</td>
            </tr>
          `).join('')}
        </table>
      `;
      container.appendChild(tab);
    }
  }
  
  function refreshAll() {
    fetch("/refresh").then(() => fetchData());
  }
  
  function refreshSheet(sheetName) {
    fetch(`/refresh?sheet=${sheetName}`).then(() => fetchData());
  }
  
  function reloadData() {
    fetch("/reload").then(() => fetchData());
  }
  
  function viewLog() {
    window.open("/log", "_blank");
  }
  
  // Auto-refresh every 60 seconds
  setInterval(fetchData, 60000);
  
  // Load on start
  fetchData();