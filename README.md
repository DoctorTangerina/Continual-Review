# Experience Factory + SWE-bench: experimento local

Esta pasta implementa uma prova de conceito de aprendizagem por memória externa
para um modelo de código com pesos congelados. Usa uma instalação **separada** de
[DoctorTangerina/SWE-bench](https://github.com/DoctorTangerina/SWE-bench), com o
repositório de tarefas [SWE-bench/swe-bench-tasks](https://github.com/SWE-bench/swe-bench-tasks).
Não altera esse checkout, não regenera automaticamente o benchmark e não instala
ferramentas comerciais ao importar o pacote.

## O que está implementado

- Resolução de issues do SWE-bench por dois caminhos, escolhidos na chave `solver`:
  - `agent` (padrão): o modelo trabalha em um loop dentro do container Docker da tarefa.
    A cada resposta envia um comando de shell e recebe a saída. Recebe só o texto da
    issue; as alterações que deixar no repositório são o patch.
  - `oneshot`: **uma chamada por tarefa**, via LiteLLM, com a issue e os arquivos que a
    correção de referência altera (configuração "oracle").
- Pacotes episódicos/semânticos com campos Goal/Question/Metric, escopo e exceções.
- Recuperação lexical determinística com teto de **caracteres**, top-k e IDs auditáveis.
- Quatro condições: `none`, `static`, `automatic`, `curated`.
- Memória inicial de aquecimento comum a todas as condições com memória.
- Consolidação entre blocos, snapshots e avaliação reservada sem atualização.
- Curadoria humana editável com aprovação, correção/rejeição, motivo e tempo medido.
- Integração com o harness do SWE-bench por processo no ambiente Python externo.
- Denominadores independentes das edições; resposta válida sem patch conta como tarefa não resolvida.
- Retomada que reutiliza etapas concluídas, limite de chamadas e bloqueio de execução concorrente.
- Vários casos de uma etapa em andamento ao mesmo tempo, por opção (`run --workers`); com modelo
  local, em um servidor Ollama iniciado com `OLLAMA_NUM_PARALLEL`.
- Relatórios descritivos por checkpoint/família e formulário de anotação direta das edições.
- Demonstração sintética e testes offline; nenhum deles comprova eficácia científica.

No `oneshot`, o acesso somente à issue e aos arquivos "oracle" é uma escolha de escopo
explícita. Ele não replica a navegação de repositório dos agentes avaliados no SWE-bench.

No `agent`, o loop é o do [mini-SWE-agent](https://github.com/SWE-agent/mini-swe-agent)
2.4.6, com os prompts de sua configuração de texto para o SWE-bench
(`swebench_backticks.yaml`). A chamada do modelo, o container e a coleta do patch são
deste projeto (ver [seção 7](#7-particularidades-do-swe-bench)). As taxas continuam não
sendo reproduções do ranking "Bash Only" do swebench.com: o modelo, o limite de passos,
o dataset e o modo de envio do comando (bloco de texto, não chamada de ferramenta) são
outros.

Em qualquer dos dois, as comparações internas entre condições mantêm o mesmo acesso ao código.

## 1. Testar o funcionamento sem API, credenciais ou Docker

Python 3.12 ou superior. Instalar o pacote com `python -m pip install -e .`: a única
dependência obrigatória é `mini-swe-agent==2.4.6`, usada pelo solver `agent` e pelos
testes dele. A demonstração usa `"solver": "oneshot"` e, como os testes, não chama
modelo, Docker nem rede. Abra o terminal nesta pasta:

```powershell
python -m unittest discover -s tests -v
python examples/run_demo.py runs/demo
python experiment.py status --run-dir runs/demo
```

Os testes do adaptador SWE-bench (`tests/test_swebench.py`) simulam o harness e não
precisam de Docker nem do checkout externo. Para rodar só eles:

```powershell
python -m unittest tests.test_swebench -v
```

`run_demo.py` usa sete tarefas sintéticas no formato do SWE-bench
(`examples/demo_tasks.jsonl`), com os arquivos "oracle" já em cache em
`examples/demo_cache/`. Um resolvedor determinístico propõe uma edição somente quando
uma experiência recuperada cita o marcador da tarefa, e um veredito sintético substitui
o harness: não há LLM, rede nem Docker. Ele preenche decisões com a identificação
`SYNTHETIC_DEMO_SCRIPT_NOT_HUMAN`, exclusivamente nessa demonstração. Esses registros
não são curadoria humana nem resultados de modelos; o helper recusa dados reais.
O relatório gerado exibe essa distinção. Reexecutar a demonstração concluída não
deve repetir chamadas. Se mudar o código depois de `prepare`, use outro nome de run.

## 2. Preparar a máquina real

Uma disposição possível é:

```text
Code/
  Final-Project/Experiments/continual_review/
  SWE-bench/
    .venv/                          # criado dentro do WSL/Linux
    swe-bench-tasks/tasks/<instance_id>/
```

A configuração usa `SWE-bench/SWE-bench_Lite`, split `test` (300 tarefas de 12
repositórios); a escolha e suas consequências estão em [docs/PROTOCOL.md](docs/PROTOCOL.md).
O harness **precisa rodar em Linux** (no Windows, dentro do WSL; ver
[Quebras de linha](#7-particularidades-do-swe-bench)). No WSL, na pasta que contém o projeto:

```bash
git clone https://github.com/DoctorTangerina/SWE-bench.git
cd SWE-bench
git checkout 02e7a74ffd0b707aab73d203fe87bdc7c76afc8e
python3 -m venv .venv
.venv/bin/pip install -e .
git clone https://github.com/SWE-bench/swe-bench-tasks.git swe-bench-tasks
git -C swe-bench-tasks checkout 3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d
```

O código desta pasta **não instala o SWE-bench nem edita as tarefas**. `catalog` baixa do
GitHub os arquivos-fonte das tarefas, e o harness baixa a imagem Docker de cada tarefa
na primeira avaliação.

Os commits inicialmente compatibilizados são:

```text
SWE-bench (harness):  02e7a74ffd0b707aab73d203fe87bdc7c76afc8e
swe-bench-tasks:      3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d
```

O script verifica esses commits e alterações em arquivos rastreados do SWE-bench. Se for
necessário usar outra revisão, primeiro revisar a compatibilidade e os dados,
fixar a nova revisão em `expected_swebench_commit`/`expected_swebench_tasks_commit` e
preparar um novo run.

Docker precisa estar funcionando e acessível pelo Python do SWE-bench (no Windows,
Docker Desktop com integração WSL). Com `solver: "agent"`, o comando `docker` também
precisa funcionar no ambiente onde este projeto roda: é ele que cria o container de
cada tentativa (em Linux, o usuário precisa estar no grupo `docker`). O harness não chama LLM nem usa credenciais. O
modelo usa LiteLLM e a credencial de seu provedor por variável de ambiente. **Não colocar
segredos no JSON de configuração, nos pacotes ou no Git.** O código não lê ou copia
o conteúdo das credenciais para os resultados.

Instalar as dependências de inferência nesta pasta, de preferência em ambiente separado:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
Copy-Item configs/swebench.example.json configs/swebench.local.json
```

Em Linux, ativar `.venv/bin/activate`. Os comandos Python seguintes são os mesmos.
O checkout SWE-bench mantém seu próprio ambiente, indicado em `swebench_python`.

Editar `configs/swebench.local.json`:

- `benchmark`: opcional; o único valor aceito é `"swebench"` (o padrão).
- `solver`: `"agent"` (padrão) ou `"oneshot"`. Todos os arquivos de `configs/` declaram o
  seu. O `agent` é recusado com o backend `demo`, com `review_runner: "claude_cli"` e sem
  `review_num_ctx`; uma configuração nesses casos precisa declarar `"solver": "oneshot"`.
- `agent_step_limit`: máximo de respostas do modelo por tentativa. Padrão `30`.
- `agent_command_timeout_seconds`: limite de um comando dentro do container. Padrão `120`.
- `agent_observation_max_chars`: máximo de caracteres da saída de um comando mostrados ao
  modelo. Padrão `4000`; acima disso ficam a primeira e a última metade e a contagem do
  que foi removido.
- `agent_oracle_paths`: `false` por padrão. Com `true`, o agente recebe também os
  **caminhos** (não o conteúdo) dos arquivos que a correção de referência altera.
- `review_num_ctx`: janela de contexto. Obrigatória para modelos locais do Ollama (enviada
  como `num_ctx`) e para o `agent` com qualquer provedor: o loop encerra a tentativa antes
  de um prompt que não caberia nela.
- `swebench_root`: checkout do SWE-bench. Alternativamente definir `SWEBENCH_ROOT`.
- `swebench_tasks`: repositório de tarefas; padrão `swe-bench-tasks` dentro do checkout.
- `swebench_python`: comando que inicia o Python onde o SWE-bench está instalado. No
  Windows precisa ser o Python **dentro do WSL**, por exemplo
  `["wsl.exe", "-d", "Ubuntu", "-e", "/mnt/c/.../SWE-bench/.venv/bin/python"]`.
- `swebench_dataset` e `swebench_split`: quais tarefas entram no catálogo.
- `swebench_eval_timeout_seconds`: limite dos testes por tarefa no harness.
- `expected_swebench_commit` e `expected_swebench_tasks_commit`: opcionais; fixam as revisões.
- `swebench_cache`: opcional; padrão `../cache/swebench`.
- `repair_enabled`: `false` desliga o harness (alvos ficam sem avaliação).
- `review_model`: ID explícito aceito pelo LiteLLM; preferir uma versão fixa.
- `review_reasoning`: `false` por padrão. Envia `enable_thinking` explicitamente ao modelo/extrator
  (alguns modelos, como Nemotron 3 no NVIDIA NIM, raciocinam por padrão). Provedores sem
  esse controle conhecido são recusados. Cada `*.usage.json` registra `reasoning_chars`.
- `review_temperature` e `review_seed`: amostragem das chamadas do modelo/extrator.
  Padrão `0` e `0`. Isso fixa a amostragem; com o Ollama a mesma entrada só repete a
  saída junto com `review_unload_model` (abaixo). `null` usa o padrão do provedor, e as
  respostas variam a cada chamada. Um provedor que não aceita o parâmetro
  é recusado; com `claude_cli` ambos precisam ser `null`. Cada `*.usage.json` registra
  os valores enviados e o `finish_reason`.
- `review_unload_model`: `true` por padrão; só vale para modelos locais do Ollama.
  Descarrega o modelo antes de cada chamada, de modo que cada prompt é calculado a partir
  de um modelo recém-carregado. Sem isso o Ollama reaproveita o estado do prompt anterior
  e a resposta ao mesmo prompt muda conforme as chamadas que vieram antes, mesmo com
  temperatura 0 (medido em [docs/PROTOCOL.md](docs/PROTOCOL.md)); o Ollama não tem opção
  para desligar esse reuso. Custa uma carga do modelo por chamada. Cada `*.usage.json`
  registra `model_unloaded_before_call`. No `agent` o modelo é descarregado **uma vez por
  tentativa**, antes do primeiro passo; os passos seguintes reaproveitam o prefixo da
  conversa (`model_unloaded_before_attempt` em `review.raw.usage.json`). Com `run --workers` maior
  que 1, o descarregamento é um por grupo de chamadas simultâneas (ver
  [seção 4](#4-executar-e-fazer-a-curadoria)).
- `review_runner`: `litellm` (padrão, API do provedor) ou `claude_cli`. Com `claude_cli`, o
  modelo/extrator roda via `claude -p` no host com o login da assinatura Claude
  (sem chave de API; `ANTHROPIC_API_KEY` é removida do ambiente da chamada). Sem
  ferramentas, MCP, configurações de usuário/projeto nem CLAUDE.md; o system prompt é o do
  experimento. O CLI não desliga o raciocínio nem aplica `max_output_tokens`, então exige
  `review_reasoning: true`; cada `*.usage.json` registra `thinking_tokens` e a versão do CLI.
  Também não controla a amostragem: `review_temperature` e `review_seed` precisam ser `null`.
  `review_model` sem prefixo, por exemplo `claude-haiku-5-5`.
- `claude_bin`: opcional; caminho do executável `claude`. Padrão: `PATH`, depois a cópia da
  extensão do VS Code (muda de versão quando a extensão atualiza; a versão fica no manifesto).
- `api_base`: opcional, para um endpoint compatível configurado no LiteLLM.
- `max_calls`: teto compartilhado do run inteiro, incluindo aquecimento e condições.
- `max_output_tokens`: teto das chamadas do modelo/extrator.
- `memory_max_chars` e `memory_top_k`: teto de caracteres e quantidade de pacotes recuperados.
- `source_split_policy`: `project` por padrão; `pr` permite estudar o mesmo projeto,
  mas não a mesma tarefa, em papéis diferentes. A escolha muda a alegação de transferência.

Os caminhos relativos da configuração são relativos ao arquivo JSON. `swebench_python`
usa caminhos do sistema onde esse Python roda (no WSL, `/mnt/c/...`). A variável
`SWEBENCH_ROOT`, quando usada, deve preferencialmente ser absoluta. Caminhos de outra
máquina não precisam ser os mesmos desta cópia do projeto; preparar o run na máquina de execução.

A credencial do modelo pode ficar em `.env` nesta pasta (ignorado pelo Git), no formato
`NOME=valor`; o programa a exporta ao iniciar, sem sobrescrever variáveis já definidas.
Para o NVIDIA NIM: `NVIDIA_NIM_API_KEY=...` e `review_model` com o prefixo `nvidia_nim/`,
por exemplo `nvidia_nim/nvidia/nemotron-3.5-lightning-30b-a3b`.

```powershell
python experiment.py --config configs/swebench.local.json doctor
```

`doctor` verifica caminhos, Git, Docker, configuração do modelo, o `solver` com os limites
do agente e a versão instalada do mini-SWE-agent, existência das
credenciais e, em `swebench_harness`, se o Python do SWE-bench importa `swebench`/`docker`
e usa quebras de linha LF, sem chamada de LLM. `dataset_exported` fica `false` até o
primeiro `catalog`. `review_credentials` mostra `temperature` e `seed`, e `sampling_pinned`
fica `false` quando algum dos dois é `null`. Ele não autentica os serviços nem comprova que cada imagem e teste
roda; isso precisa ser observado no piloto.

## 3. Escolher os casos e preparar o run

```powershell
python experiment.py --config configs/swebench.local.json catalog --output manifests/swebench_catalog.csv
```

O catálogo inclui as tarefas do dataset/split configurado, lidas do commit fixado do
repositório de tarefas. Cada tarefa tem **um** alvo: ser resolvida pelo harness. Com o
SWE-bench Lite/`test`, são 300 tarefas. A coluna `context_chars` ajuda a escolher tarefas
que cabem na janela do modelo no `oneshot`. Com `solver: "agent"` nenhum arquivo-fonte é
baixado e as colunas `context_files` e `context_chars` ficam vazias.
As exclusões por arquivo-fonte não obtido ficam em `swebench_catalog.excluded.json`. Que
o patch de referência resolve cada tarefa ainda precisa ser reproduzido no ambiente do
piloto. O importador não inventa nem revalida essa propriedade sozinho.

Copiar o CSV para `manifests/swebench_pilot.csv` e preencher:

| Campo | Preenchimento |
|---|---|
| `role` | `warmup`, `adapt`, `eval`; vazio ou `exclude` deixa fora |
| `block` | Inteiro 1, 2, ... somente para `adapt`; sem saltos |
| `family` | Família da tarefa definida antes de olhar os resultados; obrigatória (o ensaio `manifests/swebench_smoke.csv` usa `smoke`) |

Não usar a ordem do catálogo como uma amostragem justificada. Selecionar explicitamente
as instâncias, verificar duplicatas e casos derivados, decidir a ordem no CSV e registrar
a justificativa. O programa impede IDs duplicados, tarefas idênticas entre papéis e
sobreposição de projeto/tarefa segundo a política escolhida. Não detecta clones semânticos
ou issues correlatas: a auditoria dessas relações continua necessária.

O CSV define três conjuntos:

1. `warmup`: produzir pacotes aprovados para a memória inicial comum.
2. `adapt`: alimentar a memória incremental, em blocos ordenados.
3. `eval`: avaliar snapshots; nunca produzir pacotes nem feedback de treinamento.

Um piloto de seis casos reais é uma opção de viabilidade: dois de aquecimento,
dois de adaptação e dois reservados, com fontes compatíveis com a política de splits.
Ele não é o tamanho recomendado para demonstrar generalização estatística. Use casos
separados no estudo final, dimensionado depois de medir custo e variabilidade.

```powershell
python experiment.py --config configs/swebench.local.json prepare --assignments manifests/swebench_pilot.csv --run-dir runs/swebench-01
```

`prepare` não chama modelos. Ele fixa configuração, prompts/código, commits do harness
e das tarefas, dataset, identidades e hashes das tarefas congeladas, casos e ordem. Com
um modelo local, o servidor Ollama precisa estar ativo: o manifesto registra sua versão e
o digest do modelo (`runtime.ollama`), e `run` recusa a execução se o digest mudar. Não
reutiliza uma pasta existente. Para outro modelo, outra ordem, outro orçamento ou código
alterado, preparar um run novo. Não editar o manifesto para fazer um run antigo aceitar alterações.

### Ensaio do agente antes de preparar um run

`tools/agent_trial.py` roda o loop do agente nos casos `warmup` de um CSV, sem memória e
sem avaliação pelo harness, e grava as trajetórias fora de `runs/`. Serve para medir, antes
de um run, se o modelo consegue conduzir o loop e quanto custa uma tentativa: respostas com
exatamente um bloco de comando, como cada tentativa terminou, se deixou um patch, passos,
tokens e segundos. Casos `eval` nunca são executados por ele.

```powershell
python tools/agent_trial.py --config configs/swebench.local.json --output trials/agent-01
python tools/agent_trial.py --compare trials/agent-01 trials/agent-02
```

O padrão de `--assignments` é `manifests/swebench_catalog.csv`. `--compare` informa, por
caso, o primeiro passo em que duas execuções divergem.

## 4. Executar e fazer a curadoria

As quatro condições são executadas em pastas independentes, com avaliação inicial
e após cada bloco. A consolidação acontece **ao final do bloco**: os casos de um
bloco recebem o mesmo estado anterior de memória. Esse detalhe é parte do protocolo.

```powershell
python experiment.py run --run-dir runs/swebench-01 --condition none --live
```

A primeira execução produz os candidatos de aquecimento e pausa em:

```text
runs/swebench-01/warmup/curation.json
```

Para cada item, manter `candidate_id`; preencher:

- `decision`: `approve`, `correct` ou `reject`.
- `reviewer`: identificação de quem de fato fez a revisão.
- `reason`: justificativa da decisão.
- `human_seconds`: tempo humano medido, em segundos.
- `package`: editar a lição, escopo, exceções ou GQM se necessário. Uma edição de
  conteúdo requer `decision=correct`; não alterar a proveniência da experiência.

Não usar resultados reservados para escolher a decisão. Na versão implementada,
o arquivo mostra o pacote candidato; seus arquivos vizinhos contêm o episódio,
o feedback de adaptação e a saída bruta para conferência. Não há julgamento humano
automático nem aprovação implícita por decurso de tempo.

Reexecutar o mesmo comando. As etapas concluídas são reutilizadas. Em seguida:

```powershell
python experiment.py run --run-dir runs/swebench-01 --condition static --live
python experiment.py run --run-dir runs/swebench-01 --condition automatic --live
python experiment.py run --run-dir runs/swebench-01 --condition curated --live
```

Na condição `curated`, o processo pausa após cada bloco em
`conditions/curated/adapt-N/curation.json`. Preencher e reexecutar o último comando.
`automatic` usa os pacotes gerados sem curadoria adicional; `static` mantém a semente;
`none` não recupera experiência alguma. A semente aprovada é comum às três condições
com memória e tem seu custo registrado uma vez.

### Vários casos ao mesmo tempo (`--workers`)

```bash
OLLAMA_NUM_PARALLEL=2 ollama serve          # em outro terminal, antes do run
python experiment.py run --run-dir runs/swebench-01 --condition static --live --workers 2
```

`--workers N` mantém até N casos em andamento dentro de uma etapa (o aquecimento, um
bloco de adaptação ou um checkpoint). O padrão é 1, um caso depois do outro. Os casos de
uma etapa já recebem a mesma memória e nenhum lê o resultado de outro, então o
paralelismo muda o momento das chamadas, não o que é enviado.

Os casos avançam em grupos de N, na ordem do manifesto, e o grupo dá um passo de cada
vez: as N tentativas, depois as N avaliações pelo harness, depois as N extrações. A
ordem dos resultados (`candidates.json`, memória) continua a do manifesto.

Com modelo local, as N chamadas vão para o mesmo servidor Ollama, que precisa ter sido
iniciado com `OLLAMA_NUM_PARALLEL` igual ou maior que N:

- O Ollama carrega o modelo com N janelas de `review_num_ctx`, uma por chamada simultânea.
  A memória usada pelo contexto é multiplicada por N.
- A API do Ollama não informa esse valor, e um servidor com menos vagas aceita as chamadas
  e executa as excedentes depois, em uma vaga já usada. Por isso, com `--workers` maior
  que 1, o `run` mede o servidor antes do primeiro caso: descarrega o modelo, envia N
  chamadas curtas juntas e recusa a execução se o servidor não as calculou ao mesmo
  tempo. Essas chamadas não pertencem a nenhum caso e não contam no teto de chamadas.
- Com `review_unload_model = true`, o modelo é descarregado uma vez por grupo e por passo,
  e as chamadas do grupo começam juntas, cada uma em uma vaga vazia do modelo
  recém-carregado. Descarregar antes de cada chamada, como com um worker, não é possível:
  o Ollama espera as chamadas em andamento antes de descarregar.
- **A resposta a um prompt deixa de ser reprodutível.** Na medição em
  [docs/PROTOCOL.md](docs/PROTOCOL.md), um prompt que sozinho deu sempre a mesma resposta
  deu três respostas diferentes quando outra chamada era calculada ao mesmo tempo no
  servidor. Uma condição executada com mais de um worker não é comparável caso a caso com
  outra execução do mesmo prompt. O `run` avisa isso ao iniciar, e cada `*.usage.json`
  registra em `parallel_cases` quantos casos estavam em andamento junto com a chamada.
- O ganho medido nesta máquina (CPU, modelo de 7B, duas vagas, prompts de até 306 tokens)
  foi de 44 s para cerca de 31 s em duas chamadas, e de 72 s para 50 a 57 s em duas
  conversas de três passos. A leitura do prompt não ficou mais rápida no total, só a
  geração; em prompts longos o ganho deve ser menor e não foi medido.

Com provedor hospedado ou `claude_cli`, até N chamadas são feitas ao mesmo tempo; escolher
N dentro do limite de requisições do provedor, porque uma chamada recusada é uma etapa
falha.

`--workers` é argumento da execução e não entra no manifesto: pode mudar de uma execução
para outra do mesmo run. Cada execução grava uma linha `run_started` em `audit.jsonl` com
a condição e o número de workers. Os tempos por chamada medidos com mais de um worker não
são comparáveis com os medidos com um, e o `elapsed_seconds` de uma etapa passa a incluir
a espera pelos outros casos do grupo antes do descarregamento.

Para acompanhar somente progresso, sem revelar a avaliação:

```powershell
python experiment.py status --run-dir runs/swebench-01
```

O código não expõe feedback reservado ao extrator, mas os arquivos estão no disco.
Quem faz curadoria deve respeitar o embargo dos diretórios `checkpoint-*` até concluir
todas as condições. Isso é separação de fluxo de dados, não controle de acesso entre
usuários do sistema operacional.

## 5. Falhas, retomada e orçamento

Uma falha interrompe o processo. Saída bruta, arquivos intermediários e auditoria
permanecem no run. Não há repetição automática de chamada falha nem descarte silencioso
de um caso.

Uma resposta malformada do modelo (JSON inválido, sem a lista `edits`, cortada no
limite de saída) não é uma falha do run: o caso é registrado como não resolvido, com
`reason = invalid_model_output`, sem executar o harness. A chamada não é repetida:
repetir até obter um formato válido seria escolher entre amostras, o que não faz parte
do protocolo. Uma resposta malformada do extrator deixa o caso sem pacote. As contagens aparecem em `report.md` e `summary.json`. Erros de transporte,
timeout, prompt maior que `review_num_ctx` e falhas do harness continuam interrompendo
o run.

No `agent`, o que o modelo fez é sempre um resultado registrado, nunca uma falha do run.
A tentativa termina de uma destas formas (`agent.exit` em `review.json`, `agent_exit` em
`case.json`), e em todas elas o patch avaliado é o `git diff` do container naquele momento:

| Saída | Causa |
|---|---|
| `Submitted` | o modelo executou o comando de envio |
| `LimitsExceeded` | usou `agent_step_limit` respostas |
| `ContextExhausted` | o próximo prompt não caberia em `review_num_ctx` |
| `RepeatedFormatError` | 3 respostas seguidas sem exatamente um bloco de comando |
| `RepeatedCommands` | os últimos comandos são um mesmo bloco de 1 a 5 comandos repetido `agent_repeat_limit` vezes seguidas (padrão 3; 0 desliga); o comando que completaria a repetição não é executado |

Um diff vazio é registrado como `no_patch_proposed`, sem chamar o harness. Um comando que
falha ou excede o limite de tempo é informado ao modelo e o loop continua. Interrompem o
run: container que não inicia, imagem que não pode ser baixada, `docker exec` que não
executa o comando, erro ou timeout do provedor. Uma nova tentativa (`--retry-failed`)
recomeça do passo 1 em um container novo; a trajetória da tentativa falha fica em
`attempt_history/`.

Com `--workers` maior que 1, uma falha não interrompe os outros casos do grupo: eles
terminam o passo em andamento e são salvos, o grupo não avança para o passo seguinte,
nenhum grupo novo começa, e o processo para com o erro do primeiro caso que falhou na
ordem do manifesto (a mensagem diz quantos outros falharam). Cada falha tem seu
`*.failed.json` e sua linha na auditoria. O mesmo vale para o teto de chamadas atingido
no meio de um grupo: o teto não é ultrapassado. O primeiro Ctrl+C espera os casos em
andamento; o segundo abandona o processo, e até N etapas podem ficar com chamada
reservada e sem resultado, e até N containers podem permanecer. O Ctrl+C também chega
aos processos do Docker e do harness em andamento, cujas etapas ficam registradas como
falhas.

Após examinar a causa de uma falha, uma nova tentativa exige:

```powershell
python experiment.py run --run-dir runs/swebench-01 --condition curated --live --retry-failed
```

Isso reutiliza etapas bem-sucedidas e repete apenas as ainda falhas/incompletas. Cada
tentativa de resolução, extração ou avaliação pelo harness reserva uma chamada do teto.
Antes de uma repetição explícita, os artefatos da tentativa falha (inclusive
`swebench_eval/`) são preservados em `attempt_history/` dentro da pasta do evento.
Uma interrupção entre reservar a chamada e salvá-la pode deixar essa reserva consumida;
essa contagem conservadora evita repetir sem registrar. Uma chamada que chegou ao
provedor mas não foi salva pode ser repetida apenas conscientemente pelo operador.

O teto de chamadas **não é um teto monetário**: conta chamadas, não tokens, e inclui as
avaliações do harness, que não usam LLM mas consomem tempo, disco e Docker. Seus limites
de tempo são separados (`review_timeout_seconds`, `swebench_eval_timeout_seconds`).
O programa não reabastece saldo, aumenta orçamento nem compra GPU.
Medir o custo do piloto antes de preparar um estudo maior.

A pasta do run tem um bloqueio de escrita. Se um processo for interrompido externamente,
confirmar que terminou antes de remover `.run.lock`. Não executar dois processos sobre
o mesmo run. Cada avaliação usa um `run_id` próprio (`ef-...`) no nome do container, e o
adaptador tenta remover o container em caso de timeout; verificar no Docker eventuais
recursos remanescentes de uma interrupção externa.

## 6. Relatórios e avaliação direta

Somente após concluir as quatro condições:

```powershell
python experiment.py report --run-dir runs/swebench-01
```

São gerados:

- `reports/checkpoints.csv`: contagens e taxas por condição/checkpoint.
- `reports/family_matrix.json`: matriz por família para retenção e transferência.
- `reports/summary.json`: proveniência, contagens, uso registrado e curadoria.
- `reports/report.md`: leitura descritiva. Com o `agent`, inclui por checkpoint as saídas
  do loop, a mediana de passos e os tokens somados de todos os passos.
- `reports/annotations.template.json`: gabaritos e edições para anotação pós-experimento.

O denominador é o conjunto congelado de tarefas. Resposta válida sem edições, com
alguma edição que não pode ser posicionada, ou malformada (`invalid_model_output`), conta
como tarefa não resolvida e **mantém a tarefa no denominador**. Patch que não aplica e timeout dos testes também são negativos,
como na pontuação do SWE-bench. Falhas técnicas (falha de infraestrutura ou ausência de
veredito do harness) interrompem a condição e permanecem na auditoria, em vez de virar
resultados válidos ou exclusões oportunistas.

Nos arquivos e relatórios, `findings` são as edições propostas e `repair_target_rate` é a
taxa de tarefas resolvidas pelo harness. Com um alvo por tarefa, `macro_pr_rate` coincide
com ela. O relatório não infere significância, produtividade, ausência de regressões de
software ou eficácia clínica/industrial.

A anotação manual não é necessária para a métrica principal. Para avaliar as edições além
do veredito, copiar o template e anotar cada item com `status=done`, avaliador,
justificativa e índices das edições válidas/inválidas. Os índices começam em zero. Fazer a
anotação independente/cega à condição por um protocolo de amostragem e adjudicação; o
template contém metadados administrativos e não implementa sozinho o cegamento entre avaliadores.

```powershell
python experiment.py report --run-dir runs/swebench-01 --annotations runs/swebench-01/reports/annotations.json
```

A implementação não afirma que uma tarefa resolvida demonstra uma correção sem defeitos,
nem que resolver uma tarefa já conhecida demonstra transferência para um programa inédito.

## 7. Particularidades do SWE-bench

- **Entrada do modelo (`oneshot`).** Ele recebe o repositório, o texto da issue, o conteúdo
  completo dos arquivos que a correção de referência altera (configuração "oracle" do
  SWE-bench) e as experiências recuperadas. Não recebe hints, patch de referência, patch de
  testes nem nomes de testes.
- **Saída (`oneshot`).** A resposta são edições `search`/`replace`, que o programa converte
  em um patch. Se alguma edição não puder ser posicionada (texto ausente ou repetido no
  arquivo), a tentativa inteira é negativa e o harness não é chamado.
- **Entrada do modelo (`agent`).** O texto da issue, dentro do prompt do mini-SWE-agent, e
  as experiências recuperadas, em um bloco próprio que as declara dados não confiáveis. Sem
  experiências, o prompt é o texto original do mini-SWE-agent. O modelo não recebe arquivos,
  caminhos, hints, patch de referência, patch de testes nem nomes de testes. As experiências
  são recuperadas uma vez, antes do primeiro passo.
- **Container (`agent`).** Um por tentativa, da mesma imagem em que o harness avalia, sem
  rede (`--network none`) e sem pasta do host montada. O repositório fica em `/testbed`. Os
  testes que avaliam a correção não estão no container. Cada comando roda em um shell de
  login novo (ambiente `testbed`), com a entrada padrão fechada e limite de tempo aplicado
  dentro do container; mudanças de diretório e de variáveis não persistem.
- **Saída (`agent`).** O patch é sempre `git diff` do container contra o commit em que a
  tentativa começou, inclusive quando o modelo envia. O texto que o modelo imprime ao
  enviar fica na trajetória e não é avaliado. Arquivos novos não rastreados (scripts de
  reprodução, `patch.txt`) não entram no patch.
- **Arquivos de uma tentativa (`agent`).** `review.trajectory.json` (a conversa inteira,
  no formato do mini-SWE-agent, com o registro de uso de cada chamada), `review.raw.txt`
  (o patch) e `review.raw.usage.json` (totais: passos, tokens somados, maior prompt, segundos).
- **Veredito.** Cada tarefa é positiva somente quando o harness reporta `resolved`
  (todos os testes `FAIL_TO_PASS` e `PASS_TO_PASS` passam).
- **Extrator.** Só em `warmup` e `adapt`, recebe a issue, as edições, os erros de edição,
  o veredito e o patch de referência da tarefa. No `agent`, no lugar das edições recebe o
  patch final, a saída do loop, o número de passos e os comandos sem a saída deles,
  cortados em tamanhos fixos.
- **Quebras de linha.** O harness grava o patch e o script de testes com as quebras de
  linha da plataforma. Executado pelo Python do Windows, ele reprova até o patch de
  referência. `doctor` mostra `linesep_lf` e a execução recusa um ambiente CRLF.
- **Tarefas lidas do Git.** As tarefas vêm do commit do repositório de tarefas, não dos
  arquivos em disco: um checkout Windows com `autocrlf` tem CRLF nesses arquivos.
- **Cache.** `catalog` exporta as tarefas, carrega-as com o loader do SWE-bench e baixa
  do GitHub os arquivos-fonte no commit base, tudo em `cache/swebench/` (ignorado pelo Git).
- **Imagens.** A primeira avaliação de cada tarefa baixa sua imagem Docker (alguns GB); no
  `agent`, a primeira tentativa. Cada tentativa registra em `agent.image` quantos commits
  da imagem têm data posterior ao commit base e quantos remotos Git ela tem (o esperado é
  zero nos dois; `tools/run_stats.py` avisa quando não é).
- **Limitações a declarar.** No `oneshot`, os arquivos "oracle" informam ao modelo onde
  está o defeito, o que é mais fácil que a tarefa real. No `agent`, a saída dos comandos
  não é idêntica entre execuções, então duas execuções do mesmo caso podem divergir (ver
  [docs/PROTOCOL.md](docs/PROTOCOL.md)). O Lite e o Verified cobrem só 12 repositórios,
  o que restringe os splits por projeto.

O c-CRAB (revisão de diff avaliada por um agente corretor) saiu do escopo e seu código
foi removido. Configurações com `"benchmark": "ccrab"` são recusadas, e runs preparados
com ele não podem ser retomados nem relatados por esta versão.

## Estrutura

```text
configs/             exemplos de configuração e limites
examples/            demonstração sintética no formato SWE-bench; não misturar com dados reais
src/ef_review/       importação, memória, controlador, adaptadores e relatórios
tests/               testes offline de contratos e isolamento
tools/               ensaio do agente, estatísticas de um run e ajuste de catálogo à janela
trials/              trajetórias do ensaio do agente (ignorado no Git)
docs/                protocolo operacional e referência da integração
manifests/           seleção manual local (ignorada no Git)
cache/swebench/      tarefas exportadas e arquivos-fonte baixados (ignorado no Git)
runs/                estados, curadoria e resultados locais (ignorados no Git)
```

O protocolo e suas limitações estão em [docs/PROTOCOL.md](docs/PROTOCOL.md).
A teoria detalhada está em
[Notes/Experiments/Prova de conceito](../../Notes/Experiments/Prova%20de%20conceito%20-%20Experience%20Factory%20e%20Lifelong%20Learning%20com%20c-CRAB.md),
escrita para o desenho anterior com o c-CRAB; as partes sobre o benchmark não valem mais.
