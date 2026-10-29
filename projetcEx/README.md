# projetcEx — processador de CSV com agentes

Você envia um CSV de reclamações (ou cola o texto), os agentes definidos em `.claude/agents` o processam seguindo as suas regras e a política interna (RAG), e o resultado volta no formato que você configurar. O estado do trabalho e os limites verificados estão em [PLANO.md](PLANO.md).

O caderno de estudo e o tutor **não fazem mais parte do servidor**: `/` e `/api/chat` respondem 404. O servidor só expõe a página do processador, o ADR (`/adr`), as rotas `/api/proc/*`, `/api/status` e `/styles.css` (estilo base da página).

## Instalar

A orquestração dos agentes usa `langgraph`, e a política interna (RAG) usa `pypdf`, `faiss-cpu` e `fastembed`; o restante é biblioteca padrão. Instale em um ambiente virtual, a partir de `projetcEx`:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`langchain-aws` e `boto3` são opcionais, só para `--embeddings bedrock`.

## Iniciar

A partir da raiz do repositório (`FutureMinds3`):

```bash
projetcEx/.venv/bin/python projetcEx/servidor.py --provider claude      # usa ~/.claude/settings.json
# ou: projetcEx/.venv/bin/python projetcEx/servidor.py --provider openai --configure
```

Abra **http://127.0.0.1:8766/processador**. O ADR com a justificativa de custos fica em **http://127.0.0.1:8766/adr** (também linkado no topo da página).

**Primeira vez:** o servidor indexa a política antes de aceitar conexões e baixa o modelo de embeddings (~240 MB, do HuggingFace) para `projetcEx/.cache`. Isso pode levar alguns minutos e o terminal fica parado em “Indexando a política interna…”. Nas execuções seguintes o modelo vem do disco, sem rede.

Sem chave, a edição, a prévia, a política e o relatório funcionam; processar exige credencial.

| Opção | Efeito |
|---|---|
| `--provider auto\|claude\|openai` | Provedor do modelo. `claude` lê a credencial e o gateway (`ANTHROPIC_BASE_URL`) de `~/.claude/settings.json` e chama a API por HTTP; **não executa o binário `claude`** |
| `--model <nome>` | Modelo a usar (padrão: o do provedor) |
| `--configure` | Pede a chave da OpenAI com entrada oculta; não a grava |
| `--port <n>` | Porta (padrão 8766) |
| `--reload` | Desenvolvimento: reinicia o servidor quando um `.py` do projeto, o `server.py` da raiz ou a política indexada mudar. `.html`, `.js`, `.css`, agentes e regras já são lidos do disco a cada uso e não precisam de reinício. Cada reinício reindexa a política. Não combina com `--configure` |
| `--politica <arquivo>` | Política interna (`.pdf`, `.md` ou `.txt`). Padrão: `Docs - Challenge/KS_POLITICA_INTERNA (4).pdf` |
| `--embeddings local\|bedrock` | `local`: fastembed multilíngue em `.cache`. `bedrock`: Amazon Titan v2 (exige credencial AWS) |

Se a indexação falhar, o servidor sobe mesmo assim, mostra um `AVISO` no terminal e você pode enviar a política pelo painel “Política interna”.

## Fluxo da página

1. **Entrada**: escolha o arquivo, cole o conteúdo ou use o exemplo (fictício, no formato do PDF). Aceita CSV ou texto. Defina o limite de linhas; comece com 2 ou 3.
2. **Regras e formato**: escreva as regras, defina os campos de saída (nome, valores permitidos, valor padrão, limite de caracteres), as colunas do CSV a copiar e o separador.
3. **Política interna**: mostra a política indexada, permite enviar outra e buscar trechos para conferir o que o RAG recupera.
4. **Agentes**: marque os agentes das linhas, ordene, escolha o agente do relatório (opcional) e edite os arquivos.
5. **Executar**: a prévia não chama o modelo. Ela mostra o que será enviado, os avisos de esquema e o **teto de chamadas**; só então “Confirmar e processar”.

## Orquestração (LangGraph)

O lote roda em dois grafos LangGraph, definidos em `grafo.py` e montados a partir do `pipeline.json`:

- **Por reclamação:** `guardrail_entrada` → `classificador_entrada` (opcional) → (`politica_<agente>` →) um nó por agente de `agentes` → `verificar_grounding` → `guardrail_saida`.
  - `politica_<agente>` (RAG como Knowledge Source): existe para cada agente que declara `politica:` no cabeçalho. Recupera os trechos com a consulta mascarada e os entrega ao agente seguinte.
  - `verificar_grounding`: confere, sem modelo, se as seções citadas (`[2.4]`) estavam entre as recuperadas e se cada ação imediata do risco começa por uma seção. Só marca: aparece no log, nos pontos de atenção e no parecer do caso crítico (“Verificar citação: …”).
  - Arestas condicionais:
    - `bloqueada` vai para `resposta_bloqueio`;
    - `nova_tentativa`: JSON inválido volta ao próprio agente, até 2 chamadas, e reaproveita os trechos da política;
    - `sem_resposta_valida` vai para `registrar_falha`;
    - `nao_e_reclamacao`: a triagem bloqueou a mensagem.
  - Cada agente recebe em `resultados_anteriores` a saída **completa** dos anteriores (por exemplo, os `sinais` da triagem), já mascarada.
- **Do lote:** `reclamacoes` (o grafo acima, linha a linha) → `relatorio` (o `agente_relatorio`, uma vez).

Para cada chamada, o log registra o agente, a tentativa, o modelo, o tempo, a entrada e a saída. Para os agentes com política, registra também os trechos usados; o nó `politica_<agente>` registra as seções recuperadas e o tempo do RAG. Os bloqueios do guardrail de entrada aparecem como `guardrail_entrada`.

Para ver o diagrama Mermaid do pipeline atual, rode: `python grafo.py`.

## Política interna (RAG)

A política é separada em seções numeradas (por exemplo `[2.4]`), indexada com embeddings (FAISS) e recuperada por agente. O agente pede os trechos no cabeçalho do arquivo `.md`:

```
---
name: triagem
description: Classifica uma reclamação...
politica: busca, urgencia
---
```

Os trechos chegam ao prompt numerados, e o agente deve citar a seção usada. A política enviada pela tela vale até o servidor reiniciar. A consulta ao RAG usa o texto já mascarado.

## Arquivos que você edita

| Arquivo | Para quê |
|---|---|
| `.claude/agents/<nome>.md` | Um agente: cabeçalho `name` (igual ao nome do arquivo), `description` e, opcionalmente, `model` e `politica`; depois as instruções |
| `CLAUDE.md` | Contexto comum aos agentes (ative em “Enviar o CLAUDE.md aos agentes”) |
| `regras/regras.md` | Suas regras de negócio, em texto livre |
| `regras/pipeline.json` | Agentes, ordem, agente do relatório, campos de saída, colunas copiadas, palavras a ofuscar, separador |
| `exemplo_reclamacoes.csv` | CSV fictício de exemplo |
| `Docs - Challenge/` | Política interna padrão do RAG |
| `adr.html` | ADR navegável: contexto, alternativas, decisão, consequências, custos e segurança |
| `.claude/agents/prompt_agente_*.md` | Documentos de origem dos prompts (4 partes). Só a Parte 1 vai para o modelo e está copiada em `triagem.md`, `risco.md` e `relatorio.md`. Edite o agente, não o documento |

Para criar um agente, use “+ Novo agente” na página, ou crie o `.md` com o cabeçalho acima. Ele aparece na lista e pode ser marcado.

## Modelo por agente

Cada agente escolhe o seu modelo no cabeçalho, para usar um modelo leve nas tarefas simples e um maior onde o julgamento pesa:

```
---
name: risco
model: sonnet
---
```

- `haiku`, `sonnet` e `opus` são aliases: o ID vem de `ANTHROPIC_DEFAULT_<ALIAS>_MODEL` no `~/.claude/settings.json`. Qualquer outro valor é enviado como ID literal.
- Sem `model:`, o agente usa o modelo do servidor (`--model` ou o do `settings.json`).
- A prévia mostra o modelo de cada agente, e o log registra o modelo de cada chamada.
- **Hoje os três agentes usam `haiku`**, que neste ambiente aponta para `claude-haiku-4-5`. Os números de acerto e custo dos documentos `prompt_agente_*.md` foram medidos com `claude-haiku-5-5`, então podem diferir. Nada disso foi reproduzido aqui.

**Atenção ao `agente_relatorio`:** o agente do relatório (`relatorio`) roda **uma vez no fim do lote**, com só os números. Para isso, ele deve estar em `agente_relatorio` e **fora** da lista `agentes`. Se estiver nas duas, ele também roda em cada linha e o teto de chamadas sobe.

## O que o código garante (sem depender do modelo)

- Linhas com ataque de prompt, ameaça, texto curto ou longo demais são bloqueadas **antes** do modelo (regras em código), e recebem uma resposta educada sem detalhes internos.
- Com o **classificador de entrada** ligado (`classificador_entrada` em `regras/pipeline.json`, ligado por padrão), cada linha que passou pelas regras é classificada por um modelo leve em `reclamacao_valida`, `injecao`, `ameaca` ou `fora_escopo`, **antes** dos agentes. Só a reclamação válida segue; as demais viram bloqueadas com a resposta educada. Se o modelo não devolver uma classe válida (duas tentativas), a linha vira `falha` e não é processada. Custa uma chamada a mais por linha, e o log registra o passo como `classificador_entrada`.
- CPF, cartão, e-mail, telefone, “conta + número”, CEP e nome declarado (depois de “meu nome é”, “me chamo” ou “sou o(a)”) são mascarados antes de ir ao modelo e em toda saída, log e relatório.
- Campos com valores permitidos só aceitam esses valores. Qualquer outra resposta reprova a linha.
- Palavras impróprias saem ofuscadas (`m****`) e o resumo é cortado no limite configurado.
- `produto` vazio vira o valor padrão do campo (`Não Identificado` no exemplo).
- Colunas copiadas (`data_reclamacao`, `canal`, `status`) vão da entrada à saída sem passar pelo modelo.
- Erro de chave, modelo ou cota interrompe o lote na hora; 4 falhas técnicas seguidas também.
- O tom das respostas é ajustado em código: sem emojis, sem exclamações e sem CAIXA ALTA de ênfase (siglas como CPF, PIX e PROCON são preservadas). O ajuste fica registrado no log como `guardrail_saida`. É uma regra simples, não uma análise de tom.
- Se o agente de triagem concluir que a mensagem não é uma reclamação (`"bloqueado": true`), a linha vira bloqueada com a resposta educada padrão, e o agente de risco não é chamado para ela.
- Os dados do CSV são material de análise, nunca instruções.

## O que NÃO garante

- Bedrock Guardrails: o PDF o exige no nível 3 (p. 8), mas o guardrail daqui é em código (regras + classificador por modelo). A organização respondeu que o guardrail pode ser feito via código. O AI Gateway não expõe guardrails para a chave do grupo.
- Que o classificador acerte sempre: ele é um modelo. Nas 500 linhas do dataset ele bloqueou 49 (39 injeção e 10 ameaça) e aprovou 451; não há gabarito, então ataques aprovados por engano não foram medidos. Com o classificador desligado, detectar conteúdo fora do assunto volta a depender só do agente de triagem.
- Mascarar nomes citados de outra forma, ou endereços por extenso (só o CEP é mascarado).
- Que o lote completo caiba no orçamento da chave: medido com o Haiku 4.5 do gateway, cada reclamação concluída custa ~US$ 0,015 nos agentes, já com os prompts enxutos (~US$ 7,2 para as 500, contra US$ 6,54 de orçamento). A prévia limita chamadas, não dólares.
- A qualidade das respostas dos agentes, que ainda não foram testados com um modelo real.
- Que a política seja seguida à risca: o RAG entrega os trechos, mas quem decide é o modelo.

## Saídas

CSV (colunas na ordem: `id`, colunas copiadas, seus campos), JSON (resultados, falhas, bloqueadas, relatório e logs por agente), relatório HTML com gráficos e relatório Markdown.

- O agente de risco é a fonte única da escalação. Seus campos `escalacao_imediata` (`true`/`false`) e `encaminhar_para` (lista) viram as colunas `Sim`/`Não` e `área A; área B`.
- O relatório traz:
  - mensagem-chave;
  - casos críticos (risco ou urgência Crítica), com escalação imediata primeiro, encaminhamentos, ações imediatas e parecer;
  - contagem de fatores de risco e de encaminhamentos;
  - recomendações com responsável e casos;
  - avisos do agente de relatório.
- O agente de relatório recebe só o painel (contagens) e os casos críticos, com a gerência responsável pelo produto lida da seção 3 da política. Nunca recebe o texto nem o resumo das reclamações.

## Como testar

Do mais barato ao mais real. Os passos 1 a 3 não gastam tokens.

```bash
# 1. Testes automáticos (118 testes; modelo e política simulados, sem rede)
projetcEx/.venv/bin/python -m unittest discover -s projetcEx/tests -v
node --check projetcEx/processador.js

# 2. RAG: a seção esperada aparece entre as k mais similares? (baixa o modelo na 1ª vez)
projetcEx/.venv/bin/python projetcEx/tests/avaliar_rag.py
```

3. **Agentes, sem custo**: na página, passo “Executar”, clique em prévia. Nada vai ao modelo; você vê o texto exato que o primeiro agente receberá, os bloqueios e o teto de chamadas.
4. **Execução real, com custo**: com `--provider claude` e a credencial configurada, processe **2 linhas** do CSV de exemplo e confirme o teto de chamadas. O indicador “Chave configurada” só diz que existe uma credencial, não que ela funciona.

A suíte `tests/test_processador.py` copia `regras/` e `.claude/` do projeto real. Se o `pipeline.json` mudar o número de agentes, a contagem de chamadas muda e testes que esperam 5 chamadas falham.

## Colaborar

- O repositório inclui `projetcEx/.venv` por decisão do grupo. Atenção: um venv guarda caminhos absolutos e **só funciona na máquina e na pasta em que foi criado**. Se der erro ao ativá-lo, apague `projetcEx/.venv` e recrie com os comandos de "Instalar".
- `projetcEx/.cache` (modelo de embeddings, ~240 MB) **não** é versionado: é baixado no primeiro início.
- Os dois PDFs do desafio estão no repositório, que é público. Não suba dados reais de clientes: use só o dataset fornecido pelo evento.
- Rode os testes antes de abrir um pull request (comandos em "Como testar").
- Ao fim de cada rodada de mudanças, atualize este README.
