# Protocolo operacional da implementação

Estado: código de prova de conceito; resultados de modelos reais ainda não verificados.

## Escolhas implementadas

A tarefa é resolvida de uma de duas formas, fixada na chave `solver` do manifesto.

Com `solver = oneshot`, o modelo vê `repo`, `problem_statement` e o conteúdo, no commit
base, dos arquivos que a correção de referência altera (configuração "oracle"), e
responde em uma única chamada. Com `solver = agent` (o padrão), o modelo vê só o
`problem_statement` e trabalha dentro do container da tarefa (ver "Solver `agent`").
Em nenhum dos dois recebe a tarefa completa, hints, patch de referência, patch de testes
nem nomes de testes.
O recuperador recebe a mesma visão do modelo (no `agent`, `repo` e `problem_statement`). O extrator recebe feedback somente de `warmup`
e `adapt`. A família é metadado experimental e não é fornecida ao modelo.

O harness do SWE-bench roda sem alterações, a partir do commit fixado, e é o único que
decide se o patch resolve a tarefa; este código não avalia patches. Ele não recebe a
memória experimental. O alvo de cada tarefa é identificado pelo hash do ID e das listas
`FAIL_TO_PASS`/`PASS_TO_PASS`, e a tarefa avaliada é a congelada no manifesto. Os testes
são aplicados pelo harness dentro do container, depois do patch. No `oneshot`, nenhum
agente navega no repositório ou no container: o modelo produz as edições em uma única chamada.

Os pacotes têm episódio, regra, escopo, exceções e GQM. No código atual, episódio e
regra estão no mesmo registro, com proveniência; a separação é lógica. A recuperação
lexical não é um classificador treinado de aplicabilidade. O LLM recebe escopo/exceções
e precisa julgar se são pertinentes. Políticas de reconciliação automática ou invalidação
de conflitos não são implementadas; a pessoa pode corrigir/rejeitar os novos pacotes.

Os blocos são explícitos no CSV. As condições incrementais consolidam após cada bloco,
e não após cada exemplo. Isso evita divergência no fluxo de curadoria ao retomar. A
semente é aprovada uma vez e usada como memória inicial para `static`, `automatic`
e `curated`. `none` não usa essa semente. Cada condição tem estado e snapshots próprios.

O programa mede tamanho de memória em caracteres e registra tokens reportados pelo
provedor nas chamadas de revisão/extração. Não usa “caracteres” como sinônimo de “tokens”.
Pesos e versão exata do serviço/modelo não podem ser certificados pelo cliente; fixar
versões quando disponíveis e conferir o modelo retornado nos metadados do piloto.

As chamadas do modelo e do extrator usam temperatura e semente fixas
(`review_temperature = 0` e `review_seed = 0` por padrão), enviadas em cada chamada e
registradas em cada `*.usage.json` junto com o `finish_reason`. Isso é reprodutibilidade
das configurações de amostragem, não um modelo certificado como determinístico. Para
modelos locais, o manifesto registra a versão do Ollama e o digest do modelo
(`runtime.ollama`), e a execução é recusada se o digest mudar depois do `prepare`.

Com modelos locais, o modelo é descarregado antes de cada chamada
(`review_unload_model = true` por padrão), de modo que cada prompt é calculado a partir
de um modelo recém-carregado. O motivo é o comportamento medido a seguir.

Verificado em 2026-11-13 (Ollama 0.40.16, runner llamacpp, CPU, `qwen2.5-coder:7b`
Q4_K_M; runs `pinned-check-a` e `pinned-check-b`, preparados com a mesma configuração a
partir de `manifests/swebench_smoke.csv`, antes de existir o descarregamento): com
temperatura 0 e semente 0, a resposta ao mesmo prompt depende do que o servidor
processou antes.

| Prompt | Estado do servidor | Resposta |
|---|---|---|
| Resolução, 969 tokens | primeira chamada após carregar o modelo | uma resposta, 5 de 5 chamadas |
| Resolução, 969 tokens | prompt já enviado desde a carga do modelo | outra resposta, 7 de 7 chamadas |
| Extrator, 765 tokens | primeira chamada após carregar o modelo | uma resposta, 2 de 2 |
| Extrator, 765 tokens | segunda chamada após a carga, depois do prompt de resolução | outra resposta, 2 de 2 |
| Extrator, 765 tokens | prompt já enviado desde a carga do modelo | uma terceira resposta, 1 de 1 |

Cada estado se repetiu, mas os estados diferem entre si, o que é compatível com o reuso
do cache de prompt do Ollama. As duas respostas de resolução coincidem nos primeiros 102
caracteres e divergem em um único token. Sem o descarregamento, dois runs só coincidem
se o servidor partir do mesmo estado e receber a mesma sequência de chamadas, e duas
condições que enviam o mesmo prompt para um caso podem receber respostas diferentes.

O Ollama não tem opção para desligar esse reuso. Ele executa o `llama-server` do
llama.cpp com um slot; as variáveis `LLAMA_ARG_CACHE_RAM=0` e `LLAMA_ARG_CACHE_PROMPT=0`
no ambiente do servidor desligam o cache em RAM de prompts anteriores, mas o slot ainda
reaproveita o prefixo comum com a chamada anterior (968 e 3 tokens no log), e um reuso
de 3 tokens já mudou a resposta de resolução.

Com `review_unload_model = true`, no mesmo ambiente:

- A sequência resolução, resolução, extrator, resolução, extrator, resolução deu sempre
  a mesma resposta por prompt (resolução 4 de 4, extrator 2 de 2).
- Os runs `pinned-check-c` e `pinned-check-d`, preparados com a mesma configuração,
  têm `review.raw.txt` e `package.raw.txt` com o mesmo SHA-256.

O custo é uma carga do modelo por chamada, e nenhuma chamada aproveita o prefixo da
anterior. O teste usou prompts de até 969 tokens e o modelo de 7B. Não cobre prompts
longos, o modelo de 14B, GPU, outro número de threads, outras versões do Ollama nem
provedores hospedados, onde nada é descarregado.

### Vários casos ao mesmo tempo (`run --workers`)

`run --workers N` mantém até N casos de uma etapa em andamento. Não é chave de
configuração e não entra no manifesto; cada execução registra o valor em `audit.jsonl`
(`run_started`). Os casos de uma etapa recebem a mesma memória e nenhum lê o resultado
de outro, então o que é enviado em cada chamada não muda. Os casos avançam em grupos de
N, na ordem do manifesto, e o grupo dá um passo de cada vez (tentativas, avaliações,
extrações).

Com modelo local, as chamadas simultâneas vão para um único servidor Ollama iniciado com
`OLLAMA_NUM_PARALLEL` igual ou maior que N. A regra "cada chamada parte de um modelo
recém-carregado" passa a ser: o modelo é descarregado uma vez por grupo e por passo, e as
chamadas do grupo começam juntas, cada uma em uma vaga vazia. No `agent`, o
descarregamento é um por grupo de tentativas, antes do primeiro passo de todas.

**Com mais de um worker e modelo local, a resposta a um prompt deixa de ser
reprodutível.** Na medição abaixo, um prompt que deu sempre a mesma resposta sozinho deu
três respostas diferentes quando outra chamada era calculada ao mesmo tempo no mesmo
servidor. Vagas vazias no início não bastam: a resposta depende do que o servidor calcula
junto. Consequências: uma condição executada com `--workers` maior que 1 não é comparável
caso a caso com outra execução do mesmo prompt, e a discordância entre checkpoints de
`none` passa a incluir essa variação. Cada `*.usage.json` de modelo local registra
`parallel_cases` (casos em andamento junto com a chamada; 1 = sozinha).

Verificado em 2026-11-16 (Linux, Ollama 0.40.16, runner llamacpp, CPU,
`qwen2.5-coder:7b` Q4_K_M, servidor próprio em outra porta, janela de 4.096 tokens,
temperatura 0 e semente 0):

- Com `OLLAMA_NUM_PARALLEL=2` o Ollama inicia o `llama-server` com `-c 8192 -np 2`: duas
  vagas de 4.096 tokens. A memória do contexto é a janela vezes o número de vagas.
  `/api/ps` informa a janela e não o número de vagas.
- Um descarregamento pedido durante uma chamada não a interrompe: o modelo só é
  descarregado quando ela termina. Por isso não há descarregamento antes de cada chamada
  com mais de um worker.
- Uma chamada enviada depois de outra ter terminado, sem descarregamento, recebeu a vaga
  já usada (escolhida pelo prefixo comum, 15 de 37 tokens), embora a outra vaga estivesse
  vazia. Duas chamadas enviadas juntas a um modelo recém-carregado receberam cada uma
  uma vaga vazia (log do servidor, 3 de 3 pares).
- Respostas que coincidiram: dois prompts de 36 e 37 tokens (48 tokens de saída) deram,
  enviados juntos, a mesma resposta que sozinhos, em 3 de 3 pares. Pelo código do
  experimento (`call_model` com o prompt de resolução, 223 e 207 tokens de prompt, 70 e
  62 de saída), duas chamadas em um grupo deram o mesmo SHA-256 que uma de cada vez, em
  2 de 2 grupos.
- Respostas que não coincidiram: dois prompts no formato do `agent` (texto livre, 97 e 86
  tokens, até 60 de saída), chamados A e B. Sozinho, A deu a mesma resposta em 4 de 4
  chamadas e B em 2 de 2. Enviados juntos a um modelo recém-carregado, em 4 grupos, B deu
  sempre a resposta dele sozinho e A deu três respostas diferentes (a dele sozinho em 1
  dos 4). A enviado duas vezes no mesmo grupo, em 2 grupos, deu respostas diferentes entre
  as duas cópias nos dois grupos (4 chamadas, 4 respostas diferentes, uma delas a de A
  sozinho). Em conversas de três passos, a conversa de A divergiu da execução sozinha em
  3 de 3 grupos, já no primeiro passo, e a de B em 1 de 3, no terceiro passo.
- Dois servidores separados, cada um com uma vaga (o desenho de
  [PLAN_parallel_workers.md](PLAN_parallel_workers.md)): A em um servidor enquanto B ou
  outra cópia de A rodava no outro deu a resposta de A sozinho em 6 de 6 grupos. Cada
  chamada levou de 32 a 39 s, contra 17,5 s sozinha: nesta máquina dois servidores
  preservaram a resposta e não reduziram o tempo.
- Tempo com duas vagas: as duas chamadas de 223 e 207 tokens levaram 44 s uma de cada vez
  (duas cargas do modelo) e 30 a 32 s juntas (uma carga). Cada chamada, sozinha, levou 19
  a 21 s; junta, 29 a 32 s. As conversas de três passos levaram 72 s uma de cada vez e 50
  a 57 s juntas. Pelo log do servidor, a leitura do prompt não ficou mais rápida no
  total (33 a 35 tokens por segundo sozinha, 15 a 17 em cada uma das duas vagas); a
  geração ficou (7,3 tokens por segundo sozinha, 5,6 a 5,7 em cada vaga). O ganho vem da
  geração e de uma carga do modelo por grupo no lugar de uma por chamada, e deve ser
  menor em prompts longos, que não foram medidos.
- Verificação do servidor: `run` envia N chamadas curtas juntas a um modelo
  recém-carregado e compara os períodos em que o servidor calculou cada uma (fim da
  chamada menos `prompt_eval_duration + eval_duration`). Com vagas suficientes os
  períodos coincidiram; com menos vagas, pelo menos um começou quando outro terminou. O
  servidor com 1 vaga foi recusado para 2 e 3 workers, e o com 2 vagas foi aceito para 2
  e recusado para 3.

Isso cobre prompts de até 306 tokens, saídas de até 120 tokens, duas vagas e uma janela
de 4.096 tokens. Não cobre prompts longos, a janela de 32.768 tokens com duas vagas
(memória), mais de duas vagas, o modelo de 14B, GPU nem outras versões do Ollama. A
causa da variação não foi investigada; o log do servidor mostra as duas vagas calculadas
no mesmo processo. Não foi executada nenhuma tentativa real do `agent` (com container)
com mais de um worker. Os tempos por chamada medidos com mais de um worker não são
comparáveis com os medidos com um.

### Solver `agent`

O loop é o `DefaultAgent` do mini-SWE-agent 2.4.6 (a versão fica em
`runtime.mini_swe_agent` no manifesto e `run` recusa outra). Os quatro textos de prompt
(sistema, tarefa, observação e erro de formato) são os da configuração
`swebench_backticks.yaml` desse pacote, sem alteração, com duas exceções declaradas: um
bloco para as experiências recuperadas, inserido entre a issue e as instruções, e o corte
da saída dos comandos em `agent_observation_max_chars` (4.000 no lugar de 10.000). Sem
experiências, o primeiro prompt é o texto original. O hash desses textos está no
manifesto e é conferido a cada execução.

- **O que o modelo vê.** O texto da issue e, nas condições com memória, as experiências
  recuperadas uma vez, antes do primeiro passo. O nome do repositório não é informado no
  prompt (o prompt original não o inclui); o repositório está em `/testbed`.
- **Como age.** Cada resposta tem exatamente um bloco ```` ```mswea_bash_command ````.
  O comando roda em um shell de login novo, com a entrada padrão fechada e
  `agent_command_timeout_seconds` aplicado por `timeout` dentro do container.
- **Container.** Um por tentativa, da imagem em que o harness avalia a tarefa, com
  `--network none` e sem pasta do host. O modelo não pode baixar a correção publicada.
  Os testes que avaliam a correção não estão no container: o modelo pode executar código,
  mas não pode conferir sua correção contra eles.
- **O que é avaliado.** `git diff` contra o commit em que a tentativa começou, qualquer
  que tenha sido o fim do loop: envio, limite de passos, janela de contexto esgotada ou
  três respostas seguidas fora do formato. O texto que o modelo imprime ao enviar não é
  avaliado. A regra é a mesma nas quatro condições.
- **Janela de contexto.** Mensagens antigas nunca são encurtadas. Antes de cada chamada
  depois da primeira, a tentativa termina (`ContextExhausted`) se
  `prompt_tokens + completion_tokens` da última chamada, mais os caracteres novos
  divididos por 3, mais `max_output_tokens`, passar de `review_num_ctx`. As experiências
  ocupam parte da janela; se as condições com memória esgotarem a janela mais vezes que
  `none`, isso é um resultado a reportar.
- **Repetição.** Com temperatura 0, um modelo que repete o mesmo comando tende a repeti-lo
  até o limite de passos (medido com `qwen2.5-coder:7b` no aquecimento de `swebench-01`:
  10 das 12 tentativas giraram em um mesmo comando ou ciclo de 2 a 4 comandos até o passo
  30). A tentativa termina (`RepeatedCommands`) quando os últimos comandos formam um bloco
  de 1 a 5 comandos, comparados sem os espaços das pontas, repetido `agent_repeat_limit`
  vezes seguidas (padrão 3; 0 desliga). A resposta que completaria a repetição fica na
  trajetória, marcada `not_executed`, e seu comando não é executado. O patch avaliado é o
  `git diff` naquele momento, como nas outras saídas. A regra compara só os comandos, não
  as saídas: um comando repetido depois de outro que mudou o repositório também conta.
  Runs preparados antes desta regra não a usam (o código diferente impede retomá-los).

- **Descarregamento.** O modelo local é descarregado uma vez por tentativa, antes do
  primeiro passo, e não antes de cada chamada. Os passos seguintes reaproveitam no servidor
  o prefixo da conversa; cada chamada envia um `keep_alive` maior que o limite de um
  comando, para o Ollama não descarregar o modelo entre dois passos. Sem esse reuso cada
  passo releria a conversa inteira. Dentro de uma tentativa, o estado do servidor antes
  de cada passo é fixado pelos passos anteriores da própria tentativa; a chamada do
  extrator, depois dela, continua descarregando o modelo.
- **Imagem.** Cada tentativa registra quantos commits da imagem têm data posterior ao
  commit base da tarefa e quantos remotos Git ela tem (`agent.image` em `review.json`).
  Na imagem de `sympy__sympy-20590` há 58 commits fora do histórico de `HEAD` (tags de
  versões antigas), nenhum posterior ao commit base, e nenhum remoto.

Verificado em 2026-11-16 (Linux, Ollama 0.40.16, CPU, `qwen2.5-coder:7b` Q4_K_M, runs
`agent-smoke-a` e `agent-smoke-b`, preparados com a mesma configuração a partir de
`manifests/swebench_smoke.csv`, só o aquecimento, caso `sympy__sympy-20590`):

- As duas trajetórias são idênticas: 14 mensagens, 6 respostas do modelo, os mesmos
  tokens de prompt em cada passo (1.530, 2.622, 2.809, 2.909, 3.018, 3.101), e
  `package.raw.txt` com o mesmo SHA-256. O Ollama reporta o prompt inteiro em cada passo,
  não só o trecho novo, então a regra da janela de contexto usa a contagem certa.
- As 6 respostas tinham exatamente um bloco de comando. O modelo enviou sem editar nenhum
  arquivo: diff vazio, `no_patch_proposed`, harness não chamado.
- O reuso do prefixo aparece nos tempos: os passos levaram 59, 46, 16, 12, 13 e 9 s. Nesta
  máquina o modelo lê cerca de 22 tokens de prompt por segundo; reler os 2.809 tokens do
  passo 3 levaria cerca de 2 minutos, e ele levou 16 s. O log do Ollama não foi
  inspecionado (o servidor rodava em um terminal do usuário).
- Sem modelo: o patch de referência dessa tarefa, aplicado no container e lido de volta
  com o `git diff` da tentativa, foi avaliado pelo harness como `resolved` (1 de 1
  `FAIL_TO_PASS`, 21 de 21 `PASS_TO_PASS`). O diff do container é um patch que o harness aceita.

Isso cobre um caso, 6 passos e saídas curtas. Não cobre tentativas longas, comandos que
imprimem durações ou endereços de memória, o modelo de 14B, GPU nem provedores hospedados.

## População: SWE-bench Lite

O estudo usa `SWE-bench/SWE-bench_Lite`, split `test`: 300 tarefas de 12 repositórios
Python. Desde 2026-11-12, 18 delas são excluídas porque o prompt não cabe na janela de
32.768 tokens do modelo local (`tools/context_fit.py`,
`manifests/swebench_catalog.excluded.json`); restam 282. As tarefas vêm do repositório
`swe-bench-tasks` no commit `3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d` e são avaliadas
pelo harness do fork SWE-bench no commit `02e7a74ffd0b707aab73d203fe87bdc7c76afc8e`
(`expected_swebench_tasks_commit` e `expected_swebench_commit`). A exportação congelada
fica em `cache/swebench/` e cada tarefa selecionada tem seu hash no manifesto.

Cada tarefa tem um alvo: positivo somente quando o harness reporta `resolved`, isto é,
quando todos os testes `FAIL_TO_PASS` e `PASS_TO_PASS` passam com o patch do modelo.

Consequências:

- No `oneshot`, os arquivos "oracle" informam ao modelo onde está o defeito; a tarefa fica
  mais fácil que a original e as taxas não são comparáveis ao leaderboard do SWE-bench.
  No `agent` o modelo precisa encontrar o arquivo, como na tarefa original; as taxas
  continuam não sendo reproduções do leaderboard (outro modelo, limite de passos e dataset).
- A exclusão das 18 tarefas vem do prompt do `oneshot`. O `agent` não recebe arquivos e
  elas caberiam, mas o catálogo é mantido nos mesmos 282 casos para que os dois solvers
  possam ser comparados caso a caso.
- Com 12 repositórios, `source_split_policy = project` restringe muito os splits;
  declarar a política escolhida e seu efeito na alegação de transferência.
- O patch de referência resolver cada tarefa no ambiente local ainda precisa ser
  reproduzido no piloto.
- Com temperatura e semente fixas, cada caso é uma única amostra: o run não mede a
  variância de amostragem, e uma tarefa pode falhar mesmo com experiência pertinente.
  A repetição da resposta para o mesmo prompt depende de descarregar o modelo antes de
  cada chamada e foi verificada só no ambiente descrito em "Escolhas implementadas". Uma resposta malformada do modelo conta como tarefa não
  resolvida (`invalid_model_output`) e é reportada em separado.

O c-CRAB, usado no desenho anterior (revisão de diff avaliada por um agente corretor),
saiu do escopo e seu código foi removido; suas famílias A/B/C não se aplicam ao SWE-bench.
Famílias de tarefas devem ser definidas e registradas antes de olhar os resultados.

## Hipóteses e limites

As comparações investigam aquisição, transferência e retenção do sistema de memória,
com o mesmo modelo e o mesmo acesso ao código (contexto "oracle" ou container, conforme o `solver`). Não se pressupõe vantagem da EF. Regressões podem ser
interferência, recuperação inadequada, regra fora de escopo ou edição não posicionável, e não
automaticamente esquecimento dos parâmetros.

No `agent`, a saída dos comandos entra no prompt seguinte e não é idêntica entre
execuções: durações de testes, endereços de objetos, caminhos temporários e datas mudam.
Um caractere diferente muda o prompt e pode mudar o resto da tentativa. Uma diferença
entre `none` e uma condição com memória em um caso não pode, portanto, ser atribuída só
às experiências. Um caso concreto apareceu no primeiro ensaio: `ls -la` em `/testbed`
imprimia, na linha `..`, a hora em que o container foi criado. O container agora recebe na
pasta raiz a data de `/testbed`, e depois disso duas execuções do mesmo caso coincidiram
(medição acima, um caso). O tamanho dessa variação no estudo se mede pela condição `none`,
que avalia os mesmos casos com a mesma entrada em todos os checkpoints: a discordância
entre os checkpoints dela é essa variação.

Uma família nova não deve ser escolhida depois de olhar os resultados. A matriz
`family_matrix.json` apresenta os dados necessários para estudar desempenho antes/depois
e após outras famílias. O resumo de ganho inicial/final não substitui essa análise.

O protocolo exige conjunto de aquecimento separado. A proposta anterior considerava
aquecer opcionalmente; nesta implementação essa escolha foi fechada para evitar chamar
uma memória vazia de baseline estática. Os tamanhos dos conjuntos permanecem uma decisão
do pesquisador, condicionada a custo, variabilidade e pergunta.

O teste final deve ser analisado somente depois de terminar a adaptação e a curadoria
de todas as condições. O comando `report` aplica esse bloqueio lógico; o sistema de
arquivos não impede o operador de abrir resultados antes da hora. Registrar e declarar
eventuais desvios de protocolo, sem apagar dados.

## Avaliações ainda humanas

A métrica principal é o veredito do harness. Avaliar a qualidade das edições além
desse veredito exige anotação. Reexplicação/dívida de
conhecimento e contradição contextual (Q3/Q4 do artigo-base) não recebem pontuações
automáticas nesta versão. Tempo de curadoria é declarado pelo avaliador no momento
da decisão; não é inferido do intervalo entre duas execuções do programa.

Não há corretor entre o modelo e os testes, então o controle de corretor sem comentários
não se aplica. A condição `none`, com o mesmo modelo e contexto, é a referência sem memória.

## Referências técnicas inspecionadas

- [Fork SWE-bench](https://github.com/DoctorTangerina/SWE-bench/tree/02e7a74ffd0b707aab73d203fe87bdc7c76afc8e).
- [swe-bench-tasks](https://github.com/SWE-bench/swe-bench-tasks/tree/3d07b464b7b311a0cbfb5ed5b2d8a3b96f84a33d).
- `swebench.task.repo.load_task_repo`: carregamento das tarefas do commit fixado.
- `swebench.harness.run_evaluation`: aplicação do patch, testes e `report.json` com `resolved`.
- [LiteLLM input parameters](https://docs.litellm.ai/docs/completion/input): contrato
  de mensagens, limite de saída e timeout; compatibilidade final depende do modelo escolhido.

Não foi executado um experimento pago nesta preparação. O harness com Docker rodou
apenas nos ensaios de fumaça locais (`runs/swebench-smoke*`).
