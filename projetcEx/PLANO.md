# projetcEx — plano de execução

Processador de CSV do FinGuard: você envia um CSV, os agentes definidos em `.claude/agents` o processam seguindo as suas regras, e o resultado volta no formato que você definiu. Tudo desta frente fica nesta pasta; o caderno de estudo (pasta acima) não foi alterado.

Atualizado em 26/10/2026. Etapas 5 a 8 implementadas e testadas com modelo simulado. Falta a etapa 9 (README e teste real).

## 1. O que já está implementado

| # | Entrega | Arquivo | Como foi verificado |
|---|---|---|---|
| 1 | Leitura do CSV (separador `,` `;` tab, BOM, limite de 1000 linhas / 1 milhão de caracteres, coluna de id e de texto escolhíveis) | `processador.py` | testes unitários |
| 2 | Máscara de CPF, cartão, e-mail e telefone antes de enviar ao modelo e em toda saída | `processador.py` | testes unitários |
| 3 | Guardrail de entrada como primeiro passo: texto curto/longo demais e prompt injection são bloqueados e **nunca chegam ao modelo** | `processador.py` | teste: a linha bloqueada não aparece nas mensagens ao modelo |
| 4 | Agentes lidos de `.claude/agents/*.md` (formato do Claude Code: cabeçalho `name`/`description` + instruções), executados em ordem, cada um recebendo o resultado do anterior | `processador.py`, `.claude/agents/` | testes unitários |
| 5 | Regras do usuário (`regras/regras.md`) e `CLAUDE.md` entram no prompt; contrato de saída montado a partir dos campos | `processador.py` | testes unitários |
| 6 | Formato de saída configurável: campos, valores permitidos, separador; valor fora da lista reprova a linha (não é aceito em silêncio) | `regras/pipeline.json` | testes unitários |
| 7 | Edição segura dos arquivos: só `CLAUDE.md`, `regras/*` e `.claude/agents/*.md`; sem `..`, sem links simbólicos, escrita atômica | `processador.py` | testes unitários |
| 8 | Prévia sem custo: mostra linhas, bloqueios, problemas e o texto exato que o primeiro agente receberia | `servidor.py` | teste: a prévia não chama o modelo |
| 9 | Confirmação de custo: só processa se o número de chamadas confirmado for igual ao teto calculado (com 1 nova tentativa por falha) | `servidor.py` | testes HTTP |
| 10 | Execução em segundo plano com progresso, etapa atual, cancelamento e um lote por vez | `processador.py`, `servidor.py` | testes unitários e HTTP |
| 11 | Proteção de custo: erro de chave, modelo, permissão ou cota interrompe o lote na hora; 4 falhas técnicas seguidas também | `processador.py` | testes unitários |
| 12 | Downloads em CSV, JSON (com logs de entrada, saída e tempo por agente) e relatório em Markdown; células neutralizadas contra fórmula de planilha | `processador.py` | testes unitários |
| 13 | Servidor que estende o do caderno: tutor continua em `/`, processador em `/processador` | `servidor.py` | teste de regressão do chat e 17 testes do caderno |
| 14 | Interface: 4 passos (CSV, regras e formato, agentes, executar), editor de agentes e do CLAUDE.md, ordem dos agentes, tabela de resultados ao vivo | `processador.html/.css/.js` | renderizada no Chrome headless |

| 15 | Entrada no formato do PDF (p. 3): avisa sobre colunas ausentes, `id` fora de `REC…`, data fora de `AAAAMMDD`, canal e status diferentes dos exemplos. Só avisa, nunca bloqueia | `processador.py` | testes unitários e HTTP |
| 16 | `produto` vazio vira `Não Identificado` por código; valor padrão e limite de caracteres por campo | `processador.py` | testes unitários |
| 17 | Palavras impróprias ofuscadas por código (`m****`), lista básica + as suas em `improprios`; sem falso positivo em `computador` | `processador.py` | testes unitários |
| 18 | Colunas de rastreio (`data_reclamacao`, `canal`, `status`) copiadas da entrada para a saída sem passar pelo modelo | `processador.py` | teste unitário |
| 19 | Relatório gerencial: totais, distribuição por campo, críticos com parecer, pontos de atenção por regra, recomendações | `processador.py` | testes unitários |
| 20 | Agente opcional `relatorio` escreve as recomendações; só vê números e casos classificados, nunca o texto das reclamações. Se falhar, o lote conclui com uma nota | `processador.py`, `.claude/agents/relatorio.md` | testes unitários |
| 21 | Relatório `.html` com gráficos de barras (sem biblioteca, sem rede, tudo escapado) e `.md`; botão de download | `processador.py`, interface | testes + captura no Chrome |
| 22 | Guardrail de saída: máscara e ofuscação em resumo, justificativa, recomendações, logs e no que passa de um agente ao outro; resposta de bloqueio educada, sem detalhes internos; log por agente na página | `processador.py`, interface | testes sobre os 4 formatos de saída |

Dois agentes de partida (`triagem`, `risco`), um `CLAUDE.md`, regras fictícias e um `pipeline.json` com os 7 campos (categoria, produto, sentimento, urgencia, resumo, risco, justificativa) já estão criados.

### Defeitos encontrados na verificação e já corrigidos (etapas 5 a 8)
- **Vazamento real**: o texto bruto devolvido por um agente (com CPF, conta e e-mail) seguia para o agente seguinte e para o log. Só a saída final era limpa. Agora tudo que um agente produz é sanitizado antes de ser repassado ou registrado. Foi o teste sobre os quatro formatos de saída que achou isso.
- A contagem de chamadas da confirmação não incluía o agente do relatório. Agora inclui.
- Uma "captura de 390 px" parecia mostrar a página cortada. Era o Chrome headless, que impõe viewport mínima de 500 px; a página estava correta. Registrei a medição, não a impressão.

### Defeitos da primeira rodada, já corrigidos
- Erros 401/403/404 viravam 502 no servidor do caderno e não dava para distinguir chave inválida de falha passageira. O processador usa o próprio adaptador (`ModelError.fatal`).
- A confirmação de custo ignorava as novas tentativas. Agora mostra o teto real.
- A palavra `null` aparecia na ordem de execução (item `null` passado ao DOM).
- O botão "Cancelar processamento" continuava visível após o término (`.secondary` sobrescrevia `hidden`). Corrigido e medido no DOM.
- O rótulo "Regras (`regras/regras.md`)" quebrava em três linhas.

## 2. O que NÃO foi verificado
- **Nenhuma chamada real ao modelo foi feita.** Testes e navegador usaram modelo simulado. Não há evidência de que a chave, o gateway ou o modelo funcionem. O indicador "Chave configurada" só diz que existe uma credencial.
- Cliques reais de usuário: no Chrome headless só rodei um script que dispara os eventos da página. Playwright não está instalado.
- Editor de agentes, criação e apagamento de agente, reordenação e o botão Cancelar pela página: cobertos por teste de servidor, não por navegador.
- Layout em celular real (390 px): o Chrome headless só chega a 500 px. Em 500 px não há scroll horizontal, e com o corpo forçado a 390 px nenhum elemento passa da borda. Não testei em um navegador de celular.
- Qualidade das respostas dos agentes: os prompts de `triagem`, `risco` e `relatorio` são um ponto de partida meu, nunca foram testados com um modelo.
- O guardrail de entrada é por regex. Ele pega os padrões de injeção e ameaça que escrevi, mas **não detecta conteúdo fora do assunto** (p. 8: “alguém tentando usar o sistema para outros fins”). Isso pede um classificador, como o Bedrock Guardrails que o PDF exige.
- A máscara cobre CPF, cartão, e-mail, telefone e “conta + número”. Nomes de pessoas e endereços **não** são mascarados.
- Os formatos do dataset vêm do PDF. O dataset real nunca foi visto, então id, data, canal e status podem diferir.

## 3. O que falta implementar

O PDF (p. 3 a 10) define formatos de entrada e saída que o processador ainda não garante. Ordem sugerida, da mais importante para a menos:

### Etapa 5 — Formato de entrada oficial (p. 3) · FEITA
Campos do dataset: `id` (ex. `REC202600142`), `data_reclamacao` (ex. `20260115`), `canal` (SAC, Ouvidoria, Banco Central, Redes Sociais), `texto_reclamacao`, `produto` (pode estar vazio), `status` (Aberta, Em análise, Resolvida). Cerca de 500 linhas.
- [x] Validar o esquema e **avisar** sobre colunas ausentes ou valores fora do esperado, sem recusar (o dataset real ainda não chegou).
- [x] Trocar o CSV de exemplo (`exemplos/reclamacoes.csv`) por um no formato oficial, claramente fictício.
- [ ] Teste com ~500 linhas para conferir limites e tempo de prévia. (O limite é 1000 linhas; não medi o tempo com 500.)

### Etapa 6 — Garantias no código, não só no prompt (p. 5) · FEITA
Hoje só o prompt pede estas regras; um modelo pode ignorá-las.
- [x] `produto` vazio vira `Não Identificado` por código.
- [x] Palavras impróprias do resumo são ofuscadas por código (a lista de `exemplos/pipeline.py` é didática; ampliar com a do evento quando houver).
- [x] Resumo limitado por **caracteres** (400 no exemplo), não por linhas: contar linhas de um texto livre não é confiável. Decisão minha.
- [x] Colunas de rastreio (`canal`, `data_reclamacao`, `status`) copiadas da entrada para a saída, sem passar pelo modelo.

### Etapa 7 — Relatório gerencial e HTML com gráficos (p. 5 e 6) · FEITA
- [x] Dashboard: total processado, distribuição por categoria, produto e urgência.
- [x] Lista destacada de reclamações críticas, com o parecer de risco.
- [x] Recomendações: agente opcional `relatorio`; os pontos de atenção são sempre calculados por regra.
- [x] Arquivo `.html` com gráficos (nível 1), sem biblioteca externa nem rede, com todo texto escapado.
- [x] Botão "Baixar HTML" na interface.

### Etapa 8 — Guardrail de saída e rastreabilidade (p. 7, 8 e 10) · FEITA
- [x] Segunda varredura de dados sensíveis sobre todos os campos finais e sobre o relatório.
- [x] Tom: só a lista de palavras impróprias. Não há análise de tom; isso exigiria um modelo.
- [x] Resposta de bloqueio educada, em português, sem expor detalhes internos; hoje o motivo técnico aparece na tabela.
- [x] Mostrar na interface o log por agente (entrada, saída, tempo), não só no JSON.

### Etapa 9 — Fechamento
- [x] README curto desta pasta: como iniciar, o que cada arquivo faz, como criar um agente.
- [ ] Teste de navegador desta página, no estilo `tests/browser.cjs`, se Playwright for instalado.
- [~] Layout em celular: conferido até 500 px (limite do Chrome headless); 390 px reais não testados.
- [ ] **Um teste real e barato**: 2 linhas, com sua chave, depois de confirmar a prévia. Só você pode autorizar esse custo.

## 4. Fora do escopo desta pasta (decisões suas)
- **ADR em HTML navegável** e a **justificativa de custos** (p. 8): são entregas de arquitetura do desafio. Posso gerar um rascunho se pedir.
- **Bedrock Guardrails × tópicos do MS Copilot**: o PDF é inconsistente (p. 8 versus p. 9 e 10). O guardrail local desta pasta é um substituto de estudo. Confirme com a organização.
- **Nível 4 (SageMaker)**: opcional.
- **RAG da política interna**: existe uma versão didática em `exemplos/rag.py`. A política real não foi fornecida e não deve ser inventada. Integrar ao processador só faz sentido quando ela chegar.
- **Dataset real**: ainda não existe nesta máquina. Os formatos acima vêm do PDF, não de um arquivo.

## 5. Como rodar hoje

```bash
# a partir de /home/luis.souza/workspace/FutureMinds3
python3 projetcEx/servidor.py --provider claude      # usa ~/.claude/settings.json
# ou: python3 projetcEx/servidor.py --provider openai --configure
```
Abra **http://127.0.0.1:8766/processador**. O caderno e o tutor continuam em `http://127.0.0.1:8766/`.

Testes (modelo simulado, sem rede e sem custo):
```bash
python3 -m unittest discover -s projetcEx/tests -v   # 41 testes
python3 -m unittest discover -s tests                # 17 testes do caderno (regressão)
node --check projetcEx/processador.js
```
Estado atual: 41 testes do processador e 17 do caderno passam. Fluxo da página (exemplo, prévia, execução, relatório, log) rodado no Chrome headless com modelo simulado.

## 6. Arquivos

```
projetcEx/
├── PLANO.md                    este arquivo
├── CLAUDE.md                   contexto enviado aos agentes (opcional por pipeline.json)
├── .claude/agents/             triagem.md, risco.md, relatorio.md (edite ou crie os seus)
├── regras/regras.md            suas regras, em texto livre
├── regras/pipeline.json        agentes, ordem, campos de saída, separador
├── processador.py              lógica: CSV, máscara, guardrails, agentes, lote, relatório, saídas
├── exemplo_reclamacoes.csv     10 linhas fictícias no formato do PDF (inclui ataque, ameaça, produto vazio)
├── servidor.py                 rotas /api/proc/* e adaptador do modelo
├── processador.html/.css/.js   interface
└── tests/test_processador.py   41 testes
```
Arquivos temporários fora do projeto: `/tmp/pe_qa` (servidor e capturas de teste) e `/tmp/pdfx` (texto extraído do PDF). Podem ser apagados.

## 7. Observações sobre o método
- O Serena não leu nem editou nenhum arquivo desta pasta: não há language server ativo para Python/JS e a sessão não expõe `read_file`. Você autorizou as ferramentas nativas só para esta tarefa. As conclusões por busca textual (por exemplo, a procura de `null` em `processador.js`) foram feitas com grep, não com `find_referencing_symbols`.
- O texto do PDF foi extraído por um parser mínimo escrito para isso (sem `pdftoppm` nem biblioteca de PDF). Os formatos citados acima foram lidos dele, mas confirme com o PDF antes de depender de algum valor exato.
