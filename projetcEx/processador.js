"use strict";
// Todo texto do CSV, dos arquivos e do modelo entra na página por textContent, nunca por innerHTML.
const $ = id => document.getElementById(id);
const NEW_AGENT = "---\nname: novo-agente\ndescription: O que este agente faz, em uma frase.\n---\nDescreva aqui o papel do agente, o que ele deve preencher e as regras que deve seguir.\n";
const S = {pipeline: {agentes: [], agente_relatorio: "", campos: [], separador: ",", incluir_claude_md: false, copiar_colunas: [], improprios: []}, files: [], models: {padrao: "", provider: "", opcoes: []}, preview: null,
  job: null, shown: 0, timer: null, editorDirty: false, csvName: "", dirtyRules: false, format: "csv"};
let toastTimer;

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value; else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value); else node.setAttribute(key, value);
  }
  node.append(...children.flat().filter(c => c !== null && c !== undefined));
  return node;
}

function toast(message) { const box = $("toast"); box.textContent = message; box.style.display = "block"; clearTimeout(toastTimer); toastTimer = setTimeout(() => box.style.display = "none", 4500); }

async function api(path, body) {
  const options = body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)};
  let response;
  try { response = await fetch(path, {...options, signal: AbortSignal.timeout(60000)}); }
  catch { throw Error("Servidor local não conectado. Confira se o terminal ainda está rodando."); }
  let data = {};
  try { data = await response.json(); } catch {}
  if (!response.ok) throw Error(data.error || "Falha na requisição (" + response.status + ").");
  return data;
}

// ---------------------------------------------------------------- status
async function refreshStatus() {
  try {
    const data = await api("/api/status");
    $("api-status").textContent = data.configured ? "Chave configurada · " + (data.provider === "claude" ? "Claude · " : "OpenAI · ") + data.model : "Sem chave: prévia funciona, processar não";
  } catch { $("api-status").textContent = "Servidor local não conectado"; }
}

// ---------------------------------------------------------------- CSV
function detectHeader(text) {
  const first = text.replace(/^﻿/, "").split("\n", 1)[0].replace(/\r$/, "");
  const delimiter = [",", ";", "\t"].reduce((best, d) => first.split(d).length > first.split(best).length ? d : best, ",");
  const cells = []; let current = "", quoted = false;
  for (let i = 0; i < first.length; i++) {
    const c = first[i];
    if (c === '"') { if (quoted && first[i + 1] === '"') { current += '"'; i++; } else quoted = !quoted; }
    else if (c === delimiter && !quoted) { cells.push(current.trim()); current = ""; } else current += c;
  }
  cells.push(current.trim());
  return cells.filter(Boolean);
}

function refreshColumns() {
  const columns = detectHeader($("csv-text").value);
  for (const [id, label] of [["col-id", "Automática"], ["col-text", "Automática"]]) {
    const select = $(id), previous = select.value;
    select.replaceChildren(el("option", {value: "", text: label}), ...columns.map(c => el("option", {value: c, text: c})));
    select.value = columns.includes(previous) ? previous : "";
  }
  invalidatePreview();
}

function setCsv(text, name) {
  $("csv-text").value = text; S.csvName = name || ""; $("csv-name").textContent = name ? name + " · " + text.length.toLocaleString("pt-BR") + " caracteres" : "";
  refreshColumns();
}

$("csv-file").addEventListener("change", async event => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 1_000_000) { toast("O arquivo passa de 1 milhão de caracteres. Divida o CSV."); event.target.value = ""; return; }
  setCsv(await file.text(), file.name);
});
$("csv-example").addEventListener("click", async () => {
  try { const data = await api("/api/proc/exemplo"); setCsv(data.csv, data.nome + " (exemplo fictício)"); } catch (error) { toast(error.message); }
});
$("csv-text").addEventListener("input", () => { S.csvName = ""; $("csv-name").textContent = ""; refreshColumns(); });
for (const id of ["col-id", "col-text", "row-limit"]) $(id).addEventListener("input", invalidatePreview);

// ---------------------------------------------------------------- entrada em texto
function countTexto() {
  const text = $("texto-text").value.replace(/\r\n?/g, "\n").trim();
  if (!text) { $("texto-count").textContent = ""; return; }
  let blocks = text.split(/\n\s*\n/).filter(b => b.trim());
  if (blocks.length === 1) blocks = blocks[0].split("\n").filter(l => l.trim());
  $("texto-count").textContent = blocks.length + " reclamação(ões) detectada(s).";
}

function setTexto(text, name) {
  $("texto-text").value = text; $("texto-name").textContent = name ? name + " · " + text.length.toLocaleString("pt-BR") + " caracteres" : "";
  countTexto(); invalidatePreview();
}

$("texto-file").addEventListener("change", async event => {
  const file = event.target.files[0]; if (!file) return;
  if (file.size > 1_000_000) { toast("O arquivo passa de 1 milhão de caracteres. Divida o texto."); event.target.value = ""; return; }
  setTexto(await file.text(), file.name);
});
$("texto-text").addEventListener("input", () => { $("texto-name").textContent = ""; countTexto(); invalidatePreview(); });

function setFormat(format) {
  S.format = format;
  $("input-csv").hidden = format !== "csv"; $("input-texto").hidden = format !== "texto";
  $("format-csv").checked = format === "csv"; $("format-texto").checked = format === "texto";
  invalidatePreview();
}
for (const id of ["format-csv", "format-texto"]) $(id).addEventListener("change", event => { if (event.target.checked) setFormat(event.target.value); });

// ---------------------------------------------------------------- abas
const TABS = [...document.querySelectorAll("[role=tab]")];

function showTab(id, focus) {
  if (!TABS.some(t => t.dataset.tab === id)) id = TABS[0].dataset.tab;
  for (const tab of TABS) {
    const on = tab.dataset.tab === id;
    tab.setAttribute("aria-selected", String(on)); tab.tabIndex = on ? 0 : -1;
    $(tab.dataset.tab).hidden = !on;
    if (on && focus) tab.focus();
  }
  try { history.replaceState(null, "", "#" + id); } catch {}
}

for (const tab of TABS) {
  tab.addEventListener("click", () => showTab(tab.dataset.tab));
  tab.addEventListener("keydown", event => {
    const i = TABS.indexOf(tab);
    const next = {ArrowRight: i + 1, ArrowLeft: i - 1, Home: 0, End: TABS.length - 1}[event.key];
    if (next === undefined) return;
    event.preventDefault(); showTab(TABS[(next + TABS.length) % TABS.length].dataset.tab, true);
  });
}
window.addEventListener("hashchange", () => showTab(location.hash.slice(1)));

// ---------------------------------------------------------------- regras e campos
function fieldRow(field, index) {
  const set = (key, value) => { field[key] = value; S.dirtyRules = true; invalidatePreview(); };
  const allowed = el("textarea", {rows: "4", "aria-label": "Valores permitidos de " + (field.nome || "campo"), placeholder: "Um valor por linha. Vazio = texto livre."});
  allowed.value = field.valores.join("\n");
  allowed.addEventListener("input", () => set("valores", allowed.value.split("\n").map(v => v.trim()).filter(Boolean)));
  const name = el("input", {type: "text", maxlength: "41", "aria-label": "Nome do campo", placeholder: "nome_do_campo", value: field.nome});
  name.addEventListener("input", () => set("nome", name.value.trim()));
  const description = el("input", {type: "text", maxlength: "300", "aria-label": "Descrição do campo", placeholder: "Opcional", value: field.descricao});
  description.addEventListener("input", () => set("descricao", description.value));
  const fallback = el("input", {type: "text", maxlength: "80", "aria-label": "Valor padrão de " + (field.nome || "campo"), placeholder: "Se vier vazio ou o agente não souber", value: field.padrao || ""});
  fallback.addEventListener("input", () => set("padrao", fallback.value.trim()));
  const limit = el("input", {type: "number", min: "20", max: "2000", "aria-label": "Máximo de caracteres de " + (field.nome || "campo"), placeholder: "Sem limite", value: field.max_caracteres || ""});
  limit.addEventListener("input", () => set("max_caracteres", limit.value ? Number(limit.value) : null));
  return el("div", {class: "field-row"},
    el("label", {class: "field"}, "Nome", name), el("label", {class: "field"}, "Valores permitidos", allowed),
    el("label", {class: "field"}, "Instrução", description),
    el("button", {class: "secondary mini", type: "button", "aria-label": "Remover campo " + (field.nome || index + 1), onclick: () => { S.pipeline.campos.splice(index, 1); S.dirtyRules = true; renderFields(); invalidatePreview(); }}, "Remover"),
    el("div", {class: "field-extra"}, el("label", {class: "field"}, "Valor padrão", fallback), el("label", {class: "field"}, "Máximo de caracteres", limit)));
}

function renderFields() { $("fields").replaceChildren(...S.pipeline.campos.map(fieldRow)); }

$("add-field").addEventListener("click", () => { S.pipeline.campos.push({nome: "", valores: [], descricao: "", padrao: "", max_caracteres: null}); S.dirtyRules = true; renderFields(); });
$("rules-text").addEventListener("input", () => { S.dirtyRules = true; invalidatePreview(); });
$("copy-cols").addEventListener("input", () => { S.pipeline.copiar_colunas = $("copy-cols").value.split(",").map(v => v.trim()).filter(Boolean); S.dirtyRules = true; invalidatePreview(); });
$("bad-words").addEventListener("input", () => { S.pipeline.improprios = $("bad-words").value.split("\n").map(v => v.trim()).filter(Boolean); S.dirtyRules = true; invalidatePreview(); });
$("report-agent").addEventListener("change", async () => { S.pipeline.agente_relatorio = $("report-agent").value; invalidatePreview(); try { await savePipeline(); } catch (error) { toast(error.message); } });
$("separator").addEventListener("change", () => { S.pipeline.separador = $("separator").value; S.dirtyRules = true; });
$("use-claude-md").addEventListener("change", () => { S.pipeline.incluir_claude_md = $("use-claude-md").checked; S.dirtyRules = true; invalidatePreview(); });

async function savePipeline() {
  await api("/api/proc/arquivo", {path: "regras/pipeline.json", conteudo: JSON.stringify(S.pipeline, null, 2) + "\n"});
}
async function saveRules() {
  await api("/api/proc/arquivo", {path: "regras/regras.md", conteudo: $("rules-text").value});
  await savePipeline();
  S.dirtyRules = false;
}
$("save-rules").addEventListener("click", async () => {
  $("rules-status").textContent = "Salvando…";
  try { await saveRules(); $("rules-status").textContent = "Salvo em regras/regras.md e regras/pipeline.json."; }
  catch (error) { $("rules-status").textContent = ""; toast(error.message); }
});

// ---------------------------------------------------------------- agentes
async function loadFiles() {
  S.files = (await api("/api/proc/arquivos")).arquivos;
  renderAgents();
}

function agentFiles() { return S.files.filter(f => f.nome); }

// ---------------------------------------------------------------- modelo por agente
// A escolha é gravada na linha `model:` do cabeçalho de .claude/agents/<nome>.md; vazio usa o modelo do servidor.
const OUTRO = "__outro__";

async function loadModels() {
  try { S.models = await api("/api/proc/modelos"); } catch { S.models = {padrao: "", provider: "", opcoes: []}; }
}

function modelSelect(f) {
  const opcoes = S.models.opcoes, atual = f.model || "";
  const select = el("select", {class: "model-select", "aria-label": "Modelo do agente " + f.nome},
    el("option", {value: "", text: "Padrão do servidor" + (S.models.padrao ? " (" + S.models.padrao + ")" : "")}),
    ...opcoes.map(o => el("option", {value: o.valor, text: (o.rotulo || o.valor) + (o.id ? " · " + o.id : " · sem ID no settings.json")})),
    ...(atual && !opcoes.some(o => o.valor === atual) ? [el("option", {value: atual, text: atual})] : []),
    el("option", {value: OUTRO, text: "Outro modelo…"}));
  select.value = atual;
  select.addEventListener("change", async () => {
    let escolhido = select.value;
    if (escolhido === OUTRO) {
      escolhido = (prompt("ID do modelo (letras, números, . _ : / -):", atual) || "").trim();
      if (!escolhido) { select.value = atual; return; }
    }
    try {
      await api("/api/proc/modelo", {agente: f.nome, modelo: escolhido});
      f.model = escolhido; invalidatePreview(); renderAgents();
      toast(f.nome + ": " + (escolhido || "padrão do servidor"));
    } catch (error) { select.value = atual; toast(error.message); }
  });
  return el("label", {class: "model-pick"}, el("small", {text: "Modelo"}), select);
}

function renderReportSelect() {
  const select = $("report-agent"), current = S.pipeline.agente_relatorio;
  select.replaceChildren(el("option", {value: "", text: "Nenhum"}), ...agentFiles().filter(f => !S.pipeline.agentes.includes(f.nome)).map(f => el("option", {value: f.nome, text: f.nome})));
  select.value = agentFiles().some(f => f.nome === current && !S.pipeline.agentes.includes(f.nome)) ? current : "";
  if (select.value !== current) S.pipeline.agente_relatorio = select.value;
}

function renderAgents() {
  const list = agentFiles(), chosen = S.pipeline.agentes;
  renderReportSelect();
  $("agent-list").replaceChildren(...(list.length ? list.map(f => {
    const box = el("input", {type: "checkbox", "aria-label": "Usar o agente " + f.nome});
    box.checked = chosen.includes(f.nome);
    box.addEventListener("change", async () => {
      S.pipeline.agentes = box.checked ? [...chosen, f.nome] : chosen.filter(n => n !== f.nome);
      renderAgents(); invalidatePreview();
      try { await savePipeline(); } catch (error) { toast(error.message); }
    });
    return el("div", {class: "agent-item"}, el("div", {class: "grow"}, el("strong", {text: f.nome}), el("small", {text: f.descricao || "Sem descrição"})),
      modelSelect(f), el("label", {}, box, "Usar"), el("button", {class: "secondary mini", type: "button", onclick: () => openEditor(f.path)}, "Editar"));
  }) : [el("p", {class: "muted", text: "Nenhum agente em .claude/agents. Crie o primeiro."})]));
  const missing = chosen.filter(n => !list.some(f => f.nome === n));
  $("order").replaceChildren(...(chosen.length ? chosen.flatMap((n, i) => [i ? el("i", {text: "→"}) : null, el("span", {text: n + (missing.includes(n) ? " (arquivo ausente)" : "")})]).filter(Boolean) : [el("span", {text: "Nenhum agente selecionado"})]));
  $("order-edit").replaceChildren(...chosen.map((n, i) => el("div", {class: "order-row"}, el("span", {text: (i + 1) + ". " + n}),
    el("button", {type: "button", "aria-label": "Subir " + n, onclick: () => moveAgent(i, -1), ...(i === 0 ? {disabled: ""} : {})}, "↑"),
    el("button", {type: "button", "aria-label": "Descer " + n, onclick: () => moveAgent(i, 1), ...(i === chosen.length - 1 ? {disabled: ""} : {})}, "↓"))));
}

async function moveAgent(index, step) {
  const list = S.pipeline.agentes, other = index + step;
  [list[index], list[other]] = [list[other], list[index]];
  renderAgents(); invalidatePreview();
  try { await savePipeline(); } catch (error) { toast(error.message); }
}

// ---------------------------------------------------------------- editor de arquivos
async function openEditor(path, template) {
  if (S.editorDirty && !confirm("Há alterações não salvas no editor. Descartar?")) return;
  const data = template === undefined ? await api("/api/proc/arquivo?path=" + encodeURIComponent(path)) : {conteudo: template};
  $("editor").hidden = false;
  $("editor-path").value = path; $("editor-path").readOnly = template === undefined;
  $("editor-text").value = data.conteudo; S.editorDirty = false; $("editor-status").textContent = data.existe === false ? "Arquivo novo: será criado ao salvar." : "";
  $("editor-delete").hidden = !(path.startsWith(".claude/agents/") && template === undefined);
  $("editor-text").focus();
}
$("editor-text").addEventListener("input", () => { S.editorDirty = true; $("editor-status").textContent = "Alterações não salvas."; });
$("new-agent").addEventListener("click", () => openEditor(".claude/agents/novo-agente.md", NEW_AGENT).catch(e => toast(e.message)));
$("edit-claude-md").addEventListener("click", () => openEditor("CLAUDE.md").catch(e => toast(e.message)));
$("editor-close").addEventListener("click", () => { if (S.editorDirty && !confirm("Fechar sem salvar?")) return; S.editorDirty = false; $("editor").hidden = true; });
$("editor-save").addEventListener("click", async () => {
  try {
    const path = $("editor-path").value.trim();
    await api("/api/proc/arquivo", {path, conteudo: $("editor-text").value});
    S.editorDirty = false; $("editor-status").textContent = "Salvo."; $("editor-path").readOnly = true;
    $("editor-delete").hidden = !path.startsWith(".claude/agents/");
    await loadFiles(); invalidatePreview();
  } catch (error) { toast(error.message); }
});
$("editor-delete").addEventListener("click", async () => {
  const path = $("editor-path").value;
  if (!confirm("Apagar " + path + "? Não há como desfazer.")) return;
  try {
    await api("/api/proc/arquivo/apagar", {path});
    const name = path.split("/").pop().replace(/\.md$/, "");
    S.pipeline.agentes = S.pipeline.agentes.filter(n => n !== name); await savePipeline();
    S.editorDirty = false; $("editor").hidden = true; await loadFiles(); invalidatePreview();
  } catch (error) { toast(error.message); }
});

// ---------------------------------------------------------------- política interna (RAG)
function trechoItem(t) {
  const detalhe = " · origem: " + t.origem + (typeof t.score === "number" ? " (" + t.score.toFixed(2) + ")" : "");
  return el("li", {}, el("strong", {text: "[" + t.secao + "] " + t.titulo}), el("span", {class: "muted", text: detalhe}),
    t.texto ? el("details", {}, el("summary", {text: "Ver o texto enviado ao agente"}), el("p", {text: t.texto})) : null);
}

function renderPolicy(data) {
  S.policyMax = data.max_bytes || 3000000;
  const box = $("policy-info");
  if (!data.carregada) { box.replaceChildren(el("div", {class: "note warning", text: data.aviso || "A política interna não está carregada."})); return; }
  const i = data.info;
  box.replaceChildren(
    el("div", {class: "kv"}, kv(i.documento || i.nome, "documento"), kv(i.versao || "—", "versão"), kv(i.total_secoes, "seções (" + i.separacao + ")"), kv(i.hash, "hash do arquivo")),
    el("p", {class: "muted small-note", text: "Arquivo: " + i.nome + " · embeddings: " + i.embeddings}),
    el("details", {}, el("summary", {text: "Seções indexadas (" + i.total_secoes + ")"}), el("ul", {}, ...i.secoes.map(s => el("li", {text: s})))));
}

async function loadPolicy() {
  try { renderPolicy(await api("/api/proc/politica")); }
  catch (error) { $("policy-info").replaceChildren(el("div", {class: "note warning", text: error.message})); }
}

async function fileToBase64(file) {
  const bytes = new Uint8Array(await file.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(binary);
}

$("policy-file").addEventListener("change", async event => {
  const file = event.target.files[0]; event.target.value = "";
  if (!file) return;
  if (file.size > (S.policyMax || 3000000)) { toast("Arquivo grande demais (máximo de 3 MB)."); return; }
  $("policy-status").textContent = "Indexando " + file.name + "…";
  try {
    const data = await api("/api/proc/politica", {nome: file.name, conteudo_base64: await fileToBase64(file)});
    renderPolicy(data); invalidatePreview(); $("policy-results").replaceChildren();
    $("policy-status").textContent = "Política trocada: " + data.info.rotulo + ". Gere a prévia de novo.";
  } catch (error) { $("policy-status").textContent = "A política anterior continua valendo."; toast(error.message); }
});

$("policy-search").addEventListener("click", async () => {
  $("policy-search-status").textContent = "Buscando…";
  try {
    const data = await api("/api/proc/politica/buscar", {consulta: $("policy-query").value, canal: $("policy-channel").value, produto: $("policy-product").value});
    $("policy-results").replaceChildren(...data.trechos.map(trechoItem));
    $("policy-search-status").textContent = data.trechos.length + " trecho(s) de " + data.politica + ", na ordem do documento.";
  } catch (error) { $("policy-search-status").textContent = ""; toast(error.message); }
});

// ---------------------------------------------------------------- prévia
function request() {
  const limit = $("row-limit").value.trim(), limite = limit ? Number(limit) : null;
  if (S.format === "texto") return {formato: "texto", texto: $("texto-text").value, limite};
  return {csv: $("csv-text").value, coluna_id: $("col-id").value, coluna_texto: $("col-text").value, limite};
}

function invalidatePreview() {
  S.preview = null; $("preview-box").hidden = true; $("run-box").hidden = true;
  $("preview-status").textContent = "";
}

function kv(value, label) { return el("div", {}, el("strong", {text: String(value)}), el("span", {text: label})); }

$("preview").addEventListener("click", async () => {
  if (S.editorDirty) { toast("Salve ou feche o editor de arquivo antes de gerar a prévia."); return; }
  $("preview-status").textContent = "Preparando…";
  try {
    await saveRules();
    const data = await api("/api/proc/previa", request());
    S.preview = data; $("preview-status").textContent = "Prévia pronta. Nada foi enviado ao modelo.";
    const box = $("preview-box");
    box.replaceChildren(
      el("div", {class: "kv"}, kv(data.linhas, "linhas lidas"), kv(data.executaveis, "irão aos agentes"), kv(data.total_bloqueadas, "bloqueadas pelo guardrail"), kv(data.total_problemas, "com problema no CSV")),
      el("p", {class: "muted", text: "Coluna de texto: " + data.coluna_texto + " · coluna de id: " + (data.coluna_id || "número da linha") + " · agentes: " + data.agentes.map(n => n + (data.modelos && data.modelos[n] ? " (" + data.modelos[n] + ")" : "")).join(" → ") + (data.agente_relatorio ? " · relatório: " + data.agente_relatorio + (data.modelos && data.modelos[data.agente_relatorio] ? " (" + data.modelos[data.agente_relatorio] + ")" : "") : "")}),
      ...(data.avisos.length ? [el("div", {class: "note warning"}, el("strong", {text: "Conferência com o dataset do PDF (só avisos, nada foi bloqueado):"}), el("ul", {}, ...data.avisos.map(a => el("li", {text: a}))))] : []),
      ...(data.bloqueadas.length ? [el("details", {}, el("summary", {text: "Linhas bloqueadas (" + data.total_bloqueadas + ")"}), el("ul", {}, ...data.bloqueadas.map(b => el("li", {text: "id " + b.id + ": " + b.motivo}))))] : []),
      ...(data.problemas.length ? [el("details", {}, el("summary", {text: "Problemas no CSV (" + data.total_problemas + ")"}), el("ul", {}, ...data.problemas.map(b => el("li", {text: "id " + b.id + ": " + b.motivo}))))] : []),
      ...(data.politica ? [el("div", {class: "card-inner"}, el("div", {class: "eyebrow", text: "POLÍTICA " + data.politica.rotulo}),
        el("p", {class: "muted small-note", text: "Trechos recuperados para a primeira linha. Na execução, o agente de risco recebe também a seção da urgência decidida pela triagem."}),
        ...data.politica.trechos.map(a => el("div", {}, el("strong", {text: a.agente}), el("ul", {class: "trechos"}, ...a.secoes.map(trechoItem)))))] : []),
      ...(data.amostra ? [el("details", {}, el("summary", {text: "Ver exatamente o que o primeiro agente receberia"}), el("p", {class: "muted", text: "Instruções (system):"}), el("pre", {class: "sample"}, el("code", {text: data.amostra.system})), el("p", {class: "muted", text: "Mensagem (já com dados sensíveis mascarados):"}), el("pre", {class: "sample"}, el("code", {text: data.amostra.user})))] : []));
    box.hidden = false;
    $("cost-note").textContent = data.chamadas_max
      ? "Serão feitas de " + data.chamadas_base + " a " + data.chamadas_max + " chamadas ao modelo (" + data.executaveis + " linhas × " + data.agentes.length + " agentes" + (data.agente_relatorio ? " + 1 do relatório" : "") + "; o teto inclui 1 nova tentativa por falha). Cerca de " + data.tokens_entrada_estimados.toLocaleString("pt-BR") + " tokens de entrada, estimativa grosseira, mais a saída. O uso segue a cobrança da sua conta."
      : "Nenhuma linha pode ser enviada aos agentes.";
    $("run-box").hidden = !data.chamadas_max;
    $("run").textContent = "Confirmar e processar até " + data.chamadas_max + " chamadas";
  } catch (error) { $("preview-status").textContent = ""; toast(error.message); }
});

// ---------------------------------------------------------------- execução
let originColumns = [];
function resetTable(columns, origin) {
  originColumns = origin || [];
  const head = $("result-table").tHead; head.replaceChildren(el("tr", {}, ...["id", "situação", ...originColumns, ...columns, "motivo"].map(c => el("th", {text: c}))));
  $("result-table").tBodies[0].replaceChildren();
}

function addRows(rows, columns) {
  const body = $("result-table").tBodies[0];
  for (const row of rows) {
    body.append(el("tr", {}, el("td", {text: row.id}), el("td", {}, el("span", {class: "badge-" + row.status, text: row.status})),
      ...originColumns.map(c => el("td", {text: (row.origem && row.origem[c]) || ""})),
      ...columns.map(c => el("td", {text: row.campos ? row.campos[c] : ""})), el("td", {text: row.status === "bloqueada" && row.resposta ? row.resposta : (row.motivo || "")})));
  }
}

function render(snapshot) {
  const running = snapshot.status === "executando";
  $("job-progress").max = Math.max(snapshot.total, 1); $("job-progress").value = snapshot.feitos;
  const names = {executando: "Processando", concluido: "Concluído", cancelado: "Cancelado", erro: "Interrompido"};
  $("job-text").textContent = names[snapshot.status] + " · " + snapshot.feitos + " de " + snapshot.total + " linhas · " + snapshot.chamadas + " de até " + snapshot.chamadas_max + " chamadas"
    + (running && snapshot.etapa ? " · agora: " + snapshot.etapa : "") + (snapshot.erro ? " · " + snapshot.erro : "");
  $("cancel").hidden = !running; $("run").disabled = running;
  $("downloads").hidden = running || snapshot.feitos === 0;
}

async function loadLogs() {
  const data = await api("/api/proc/logs?job=" + encodeURIComponent(S.job) + "&desde=" + (S.logShown || 0));
  const body = $("log-table").tBodies[0];
  for (const item of data.itens) body.append(el("tr", {}, el("td", {text: item.agente}), el("td", {text: item.id}), el("td", {text: item.duracao_s + " s"}), el("td", {text: item.erro || ""}), el("td", {text: (item.trechos || []).map(t => t.secao).join(", ")}), el("td", {}, el("code", {text: item.entrada})), el("td", {}, el("code", {text: item.saida}))));
  S.logShown = (S.logShown || 0) + data.itens.length;
  $("log-count").textContent = data.total + " chamada(s) registrada(s). Entradas e saídas aparecem mascaradas e truncadas.";
}

function stopPolling() { clearTimeout(S.timer); S.timer = null; }

async function poll() {
  try {
    const snapshot = await api("/api/proc/estado?job=" + encodeURIComponent(S.job) + "&desde=" + S.shown);
    addRows(snapshot.novos, snapshot.colunas); S.shown += snapshot.novos.length; render(snapshot);
    await loadLogs();
    if (snapshot.status === "executando") S.timer = setTimeout(poll, 1000);
  } catch (error) {
    $("job-text").textContent = error.message; $("run").disabled = false; $("cancel").hidden = true; stopPolling();
  }
}

$("run").addEventListener("click", async () => {
  if (!S.preview || S.timer) return;
  $("run").disabled = true;
  try {
    const snapshot = await api("/api/proc/iniciar", {...request(), confirmar_chamadas_max: S.preview.chamadas_max});
    S.job = snapshot.job; S.shown = 0; resetTable(snapshot.colunas, S.preview.copiar); $("progress-box").hidden = false; $("log-table").tBodies[0].replaceChildren(); S.logShown = 0; $("logs-box").hidden = false;
    for (const [id, format] of [["dl-csv", "csv"], ["dl-json", "json"], ["dl-html", "html"], ["dl-md", "md"]]) $(id).href = "/api/proc/baixar?job=" + encodeURIComponent(S.job) + "&formato=" + format;
    render(snapshot); stopPolling(); poll();
  } catch (error) { $("run").disabled = false; toast(error.message); }
});

$("cancel").addEventListener("click", async () => {
  try { render(await api("/api/proc/cancelar", {job: S.job})); } catch (error) { toast(error.message); }
});

// ---------------------------------------------------------------- início
async function init() {
  showTab(location.hash.slice(1)); refreshStatus(); loadPolicy();
  try {
    const [rules, pipeline] = await Promise.all([api("/api/proc/arquivo?path=regras/regras.md"), api("/api/proc/arquivo?path=regras/pipeline.json")]);
    $("rules-text").value = rules.conteudo;
    if (pipeline.existe) { try { const data = JSON.parse(pipeline.conteudo); S.pipeline = {agentes: data.agentes || [], agente_relatorio: data.agente_relatorio || "", campos: (data.campos || []).map(c => ({padrao: "", max_caracteres: null, ...c})), separador: data.separador || ",", incluir_claude_md: data.incluir_claude_md === true, copiar_colunas: data.copiar_colunas || [], improprios: data.improprios || []}; } catch { toast("regras/pipeline.json está inválido; defina o formato e salve."); } }
    $("separator").value = S.pipeline.separador; $("use-claude-md").checked = S.pipeline.incluir_claude_md;
    $("copy-cols").value = S.pipeline.copiar_colunas.join(", "); $("bad-words").value = S.pipeline.improprios.join("\n");
    renderFields(); await loadModels(); await loadFiles();
  } catch (error) { toast(error.message); }
}
window.addEventListener("beforeunload", event => { if (S.editorDirty || S.dirtyRules) event.preventDefault(); });
init();
