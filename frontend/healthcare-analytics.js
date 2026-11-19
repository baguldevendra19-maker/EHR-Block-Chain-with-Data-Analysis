const ANALYTICS_API_URL = 'http://127.0.0.1:5000/api/analytics/healthcare';
const analyticsForm = document.querySelector('#analytics-filter');
const periodSelect = document.querySelector('#period-select');
const anchorControl = document.querySelector('#anchor-control');
const startControl = document.querySelector('#start-control');
const endControl = document.querySelector('#end-control');
const filterError = document.querySelector('#filter-error');
const loadingMessage = document.querySelector('#loading-message');
const emptyMessage = document.querySelector('#empty-message');
const chartInstances = new Map();
const chartColors = ['#16a99d', '#2e75d2', '#f0a35e', '#8265c7', '#dc7182', '#57a67c', '#d2bd4b', '#538c9c'];

function updateFilterControls() {
  const isCustom = periodSelect.value === 'custom';
  anchorControl.hidden = isCustom;
  startControl.hidden = !isCustom;
  endControl.hidden = !isCustom;
  document.querySelector('#start-date').required = isCustom;
  document.querySelector('#end-date').required = isCustom;
}

function renderChart(canvasId, items, type, label, options = {}) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) throw new Error(`Missing chart canvas: ${canvasId}`);

  const previousChart = chartInstances.get(canvasId);
  if (previousChart) previousChart.destroy();

  const pieChart = type === 'pie';
  const horizontal = options.indexAxis === 'y';
  const dataset = {
    label,
    data: items.map((item) => item.value),
    backgroundColor: pieChart
      ? items.map((_, index) => chartColors[index % chartColors.length])
      : type === 'line' ? 'rgba(22, 169, 157, 0.16)' : '#16a99d',
    borderColor: type === 'line' ? '#16a99d' : '#ffffff',
    borderWidth: type === 'line' ? 2 : 1,
    borderRadius: type === 'bar' ? 4 : 0,
    pointRadius: type === 'line' ? 3 : 0,
    fill: type === 'line',
    tension: type === 'line' ? 0.3 : 0,
  };
  chartInstances.set(canvasId, new Chart(canvas, {
    type,
    data: {
      labels: items.map((item) => item.label),
      datasets: [dataset],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      indexAxis: horizontal ? 'y' : 'x',
      plugins: {
        legend: {
          display: pieChart,
          position: 'bottom',
          labels: { color: '#60787d', boxWidth: 11, padding: 14, font: { family: 'DM Sans', size: 10 } },
        },
        tooltip: {
          callbacks: {
            label(context) {
              const value = context.parsed.y ?? context.parsed.x ?? context.parsed;
              return ` ${label}: ${Number(value).toLocaleString()}`;
            },
          },
        },
      },
      scales: pieChart ? {} : {
        x: {
          beginAtZero: true,
          grid: { display: horizontal },
          ticks: { color: '#819397', precision: 0, font: { size: 9 } },
          title: {
            display: horizontal ? Boolean(options.valueTitle) : Boolean(options.categoryTitle),
            text: horizontal ? options.valueTitle : options.categoryTitle,
            color: '#60787d',
          },
        },
        y: {
          beginAtZero: true,
          grid: { display: !horizontal, color: '#edf2f1' },
          ticks: { color: '#819397', precision: horizontal ? 0 : undefined, font: { size: 9 } },
          title: {
            display: horizontal ? Boolean(options.categoryTitle) : Boolean(options.valueTitle),
            text: horizontal ? options.categoryTitle : options.valueTitle,
            color: '#60787d',
          },
        },
      },
    },
  }));
}

function formatCurrency(value) {
  return value == null
    ? '—'
    : Number(value).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function renderDashboard(data) {
  document.querySelector('#kpi-total').textContent = Number(data.kpis.total_records).toLocaleString();
  document.querySelector('#kpi-age').textContent = data.kpis.average_age == null ? '—' : data.kpis.average_age;
  document.querySelector('#kpi-admissions').textContent = Number(data.kpis.total_admissions).toLocaleString();
  document.querySelector('#kpi-billing').textContent = formatCurrency(data.kpis.average_billing);
  document.querySelector('#selected-period').textContent =
    `${data.period.type[0].toUpperCase()}${data.period.type.slice(1)}: ${data.period.start_date} to ${data.period.end_date}`;
  document.querySelector('#available-dates').textContent =
    `Dataset dates: ${data.period.available_start_date} to ${data.period.available_end_date}`;

  renderChart('chart-conditions', data.charts.medical_conditions, 'bar', 'Admissions', {
    indexAxis: 'y',
    categoryTitle: 'Medical condition',
    valueTitle: 'Admissions',
  });
  renderChart('chart-gender', data.charts.gender, 'pie', 'Records');
  renderChart('chart-age', data.charts.age_groups, 'bar', 'Patients', {
    categoryTitle: 'Age group',
    valueTitle: 'Patients',
  });
  renderChart('chart-monthly', data.charts.monthly_admissions, 'line', 'Admissions', {
    categoryTitle: 'Month',
    valueTitle: 'Admissions',
  });
  renderChart('chart-admission', data.charts.admission_types, 'bar', 'Admissions', {
    indexAxis: 'y',
    categoryTitle: 'Admission type',
    valueTitle: 'Admissions',
  });
  renderChart('chart-billing', data.charts.billing_amounts, 'bar', 'Records', {
    categoryTitle: 'Billing amount range',
    valueTitle: 'Records',
  });

  if (!document.querySelector('#anchor-date').value) {
    document.querySelector('#anchor-date').value = data.period.available_end_date;
  }
  if (!document.querySelector('#start-date').value) {
    document.querySelector('#start-date').value = data.period.available_start_date;
  }
  if (!document.querySelector('#end-date').value) {
    document.querySelector('#end-date').value = data.period.available_end_date;
  }
  emptyMessage.hidden = !data.empty;
}

async function loadAnalytics(formData = new FormData()) {
  filterError.hidden = true;
  filterError.textContent = '';
  loadingMessage.hidden = false;
  const submitButton = analyticsForm.querySelector('button[type="submit"]');
  submitButton.disabled = true;
  try {
    const period = formData.get('period') || periodSelect.value;
    const params = new URLSearchParams({ period });
    if (period === 'custom') {
      params.set('start_date', formData.get('start_date') || document.querySelector('#start-date').value);
      params.set('end_date', formData.get('end_date') || document.querySelector('#end-date').value);
    } else {
      const anchor = formData.get('anchor_date') || document.querySelector('#anchor-date').value;
      if (anchor) params.set('anchor_date', anchor);
    }
    const response = await fetch(`${ANALYTICS_API_URL}?${params}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `Analytics API returned HTTP ${response.status}.`);
    renderDashboard(data);
  } catch (error) {
    filterError.textContent = error.message || 'Unable to load healthcare sample analytics.';
    filterError.hidden = false;
  } finally {
    loadingMessage.hidden = true;
    submitButton.disabled = false;
  }
}

periodSelect.addEventListener('change', updateFilterControls);
analyticsForm.addEventListener('submit', (event) => {
  event.preventDefault();
  if (!analyticsForm.reportValidity()) return;
  if (periodSelect.value === 'custom' &&
      document.querySelector('#start-date').value > document.querySelector('#end-date').value) {
    filterError.textContent = 'Start date must be on or before end date.';
    filterError.hidden = false;
    return;
  }
  loadAnalytics(new FormData(analyticsForm));
});

updateFilterControls();
loadAnalytics();
