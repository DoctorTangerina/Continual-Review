# TODO — Arthur Sardella: IST e TMLR

Data da inspeção: 19/10/2026. Documento de planejamento; não há resultados experimentais verificados neste material. Os recortes e critérios abaixo são recomendações de desenho, não exigências numéricas das venues nem garantias de aceitação.

## 1. O que foi encontrado

Na pasta indicada há seis arquivos Markdown: uma síntese do projeto e cinco notas sobre Experience Factory, QIP, Lifelong Learning e memórias paramétrica e semântica. Não foram encontrados nessa pasta código, dataset, logs, resultados, manuscrito integral do TCC, PDF do artigo submetido ou pareceres do SBES. Isso descreve o conteúdo inspecionado; não estabelece que esses materiais não existam em outro lugar.

O resumo local registra uma proposta de adaptação de EF/QIP para agentes com pesos congelados e curadoria humana. Também registra recusa de um vision paper no SBES, sem fornecer os pareceres. Há uma direção experimental de revisão de código com mutantes e c-CRAB, ainda sem resultados anexos. Marcadores como `[cite: 1]` não têm uma lista de referências resolvida nesse arquivo.

Nesta conversa, em outputs/, já existem arquitetura em PNG/SVG/Mermaid, explicação e BibTeX com sete referências. A inspeção atual confirmou esses arquivos e o estado de proposta do texto; eles não constituem implementação ou evidência de eficácia.

**Diagnóstico:** existe uma base conceitual e um desenho inicial de prova de conceito. O próximo passo é produzir evidência e verificar o diferencial científico. A divisão em dois artigos é condicional: cada um precisa responder uma pergunta própria com resultados próprios.

## 2. Divisão proposta

| Dimensão | Artigo IST | Artigo de IA — TMLR como alvo recomendado |
|---|---|---|
| Unidade de contribuição | Processo de engenharia e capitalização de experiências | Comportamento e desenho da aprendizagem por memória |
| Pergunta central candidata | Quando a curadoria de experiências melhora a revisão e qual é seu custo humano? | Quando regras contextualizadas preservam transferência e retenção sob conflito e orçamento limitado de contexto? |
| Papel de EF/QIP | Estrutura central do processo | Contexto motivador; não depender do rótulo EF |
| Evidência principal | Revisões reais, defeitos, falsos positivos, esforço e rastreabilidade | Sequências controladas, interferência, recuperação, representação, ablações e generalização |
| Resultado esperado como produto | Processo executável, avaliação empírica e recomendações para revisão | Estudo explicativo ou política de memória testada contra alternativas |
| Ponto de partida | Mais diretamente conectado às notas existentes | Exige esclarecer o diferencial em relação aos trabalhos próximos |

O IST inclui processos, qualidade, testes e estudos empíricos de engenharia de software em seu escopo. Esse é o motivo da recomendação de um artigo empírico sobre revisão, além de uma adaptação conceitual. [Escopo oficial da Elsevier](https://shop.elsevier.com/journals/information-and-software-technology/0950-5849).

TMLR é uma recomendação de encaixe para o segundo artigo, com submissão contínua. Seus critérios priorizam evidência convincente e interesse para a comunidade; uma nova arquitetura ou novo algoritmo não é obrigatório. Um estudo que ofereça entendimento generalizável sobre aprendizagem por memória pode ser adequado, mas uma simples reprodução sem novos aprendizados pode não ser. [Página da TMLR](https://jmlr.org/tmlr/); [critérios](https://jmlr.org/tmlr/acceptance-criteria.html).

## 3. P0 — tarefas comuns que vêm primeiro

P0 = resolver antes da execução principal. P1 = necessário para sustentar o artigo planejado. P2 = extensão após o núcleo estar válido. Os responsáveis são sugestões; nenhuma tarefa foi enviada a outra pessoa.

- [ ] **C01 · P0 · Reunir o material integral.** Localizar TCC, manuscrito SBES, referências completas, pareceres e eventual repositório. Entrega: inventário com caminhos e versões, separando o que existe do que continua ausente. Responsáveis sugeridos: Arthur e orientadores.
- [ ] **C02 · P0 · Fechar a rastreabilidade bibliográfica.** Substituir os marcadores `[cite: ...]` por chaves verificadas; conferir as passagens usadas de Basili e Zheng. Entrega: `.bib` e tabela afirmação–fonte–seção/página.
- [ ] **C03 · P0 · Corrigir afirmações excessivas das notas.** Revisar “perspectiva inexplorada”, “agentes stateless”, “erros em produção inviáveis” e a inferência de que Xiong exigiria auditoria humana. Delimitar o agente avaliado e apresentar curadoria humana como intervenção a testar. Entrega: lista de correções justificadas, preservando as notas originais.
- [ ] **C04 · P0 · Montar matriz de trabalhos próximos.** Comparar problema, memória, feedback, curadoria, recuperação, orçamento, tarefas sequenciais e retenção. Começar pelas referências da seção 8 e ampliar pelas citações relevantes. Entrega: matriz que permita dizer o que cada artigo acrescenta; a busca inicial não é uma revisão sistemática.
- [ ] **C05 · P0 · Decidir a separação científica.** Registrar uma pergunta e uma contribuição candidata por artigo, identificando resultados exclusivos. Se a contribuição de IA se limitar a renomear a EF ou repetir resultados do IST, redesenhar ou consolidar em um artigo. Entrega: uma página com as duas perguntas, fronteiras e riscos.
- [ ] **C06 · P0 · Congelar o protocolo antes do conjunto final.** Definir unidade de análise, splits, famílias, ordem das tarefas, métricas, critérios de anotação, exclusões, versões e política de memória. Entrega: protocolo datado; parâmetros escolhidos no desenvolvimento, não nos resultados finais.
- [ ] **C07 · P0 · Construir um piloto separado.** Usar seis casos para conferir testes, revisão, anotação, memória e custo. Entrega: execução completa de ponta a ponta e planilha ou log de tokens, duração e minutos humanos. Os seis casos não entram no teste final.
- [ ] **C08 · P0 · Implementar a infraestrutura mínima.** Revisor único, recuperador, pacote de experiência, curadoria, armazenamento local, snapshots e executor de avaliação. Entrega: execução reproduzível; pesos, instruções e ferramentas fixos. Não introduzir um MACS completo apenas para reproduzir o vocabulário da visão original.
- [ ] **C09 · P0 · Impedir vazamento.** Separar programas/funções de origem e derivados entre adaptação, desenvolvimento e teste; ocultar gabaritos e resultados finais do curador; reiniciar o histórico a cada caso. Entrega: manifesto dos splits, auditoria de duplicatas e teste de avaliação sem escrita na memória.
- [ ] **C10 · P0 · Definir os controles.** Sem memória; memória estática; memória incremental automática; memória incremental curada. Igualar acesso ao código, feedback básico, ferramentas e teto de contexto. Medir diferenças reais de tokens e quantidade de pacotes. Entrega: configuração documentada por condição.
- [ ] **C11 · P0 · Definir avaliação humana e análise.** Rubrica para localização e substância do achado; avaliador sem conhecer a condição; amostra avaliada por segunda pessoa e adjudicação. Distinguir anotações dos autores de estudo sobre participantes humanos e verificar o procedimento institucional antes de recrutar participantes. Entrega: guia de anotação e estratégia de análise.
- [ ] **C12 · P0 · Dimensionar depois do piloto.** Medir variabilidade e custo, então definir quantidade de programas, projetos, ordens e repetições. O desenho anterior de 60 casos é piloto/prova de conceito, sem poder ou validade externa estabelecidos. Entrega: orçamento máximo e escopo cabível; nenhuma contratação de GPU é pressuposta.

## 4. P1 — artigo IST

Título de trabalho: **Human-Curated Experience Reuse for Continually Improving LLM-Based Code Review**. O verbo e a força da alegação devem ser ajustados ao que os resultados sustentarem.

### Perguntas candidatas

- IST-RQ1: a curadoria melhora a detecção e reduz alertas indevidos em comparação com memória automática, estática e ausência de memória?
- IST-RQ2: os ganhos persistem em revisões posteriores e em projetos diferentes dos que originaram as experiências?
- IST-RQ3: qual o esforço de curadoria e quais decisões — aprovar, corrigir, restringir ou rejeitar — explicam ganhos e falhas?

### TODO

- [ ] **I01 · P1 · Operacionalizar EF/QIP.** Mapear caracterização, metas, execução, análise e empacotamento para artefatos observáveis. Entrega: processo com entradas, saídas, responsáveis, regras de decisão e rastreabilidade; dependência C05.
- [ ] **I02 · P1 · Definir a população de revisões.** Selecionar linguagem e PRs reais com critérios explícitos, diversidade entre projetos e defeitos verificáveis. Mutantes complementam o controle experimental; não substituem toda a avaliação real. Entrega: manifesto de inclusão, exclusão e licenças; dependências C06 e C12.
- [ ] **I03 · P1 · Validar o oráculo.** Verificar testes e defeitos alvo; avaliar comentários pelo conteúdo, não só pela similaridade textual. Não classificar todo achado ausente da revisão humana como falso positivo. Entrega: gabarito adjudicado, incluindo tratamento de achados adicionais; dependências C09 e C11.
- [ ] **I04 · P1 · Avaliar a viabilidade do c-CRAB.** Executar um pequeno subconjunto, verificar ambientes e registrar casos indisponíveis. Se houver corretor no circuito, mantê-lo fixo e incluir controle sem comentários para medir o que resolve sozinho. Entrega: decisão de adotar ou não o benchmark e documentação das alterações.
- [ ] **I05 · P1 · Executar as quatro condições.** Medir a condição curada contra os controles C10, antes/depois de etapas. Para perguntas de retenção usar casos antigos; para transferência usar programas inéditos. Entrega: resultados por caso, projeto e checkpoint; dependências I02–I04 e C08–C11.
- [ ] **I06 · P1 · Medir esforço e benefício.** Registrar tempo de aprovação/correção/rejeição, tokens e latência; reportar benefício incremental e custo por unidade útil. Se não houver observação de equipe em produção, não inferir produtividade de equipe, economia organizacional ou adoção industrial. Entrega: curvas de benefício–esforço.
- [ ] **I07 · P1 · Examinar o mecanismo de curadoria.** Auditar amostra de pacotes e trajetórias de erro. Quando viável, comparar filtros de aceitação/rejeição com o processo que também corrige o conteúdo. Entrega: distinção entre selecionar melhor conteúdo e acrescentar informação humana.
- [ ] **I08 · P1 · Analisar sem pseudorreplicação.** Respeitar o agrupamento por programa e projeto; reportar diferenças, incerteza e dispersão. Ordens e chamadas repetidas sobre o mesmo programa não aumentam artificialmente o número de casos independentes. Entrega: análise compatível com a unidade de amostragem.
- [ ] **I09 · P1 · Escrever a contribuição empírica.** Introdução com lacuna delimitada; EF/QIP e relacionados; processo; desenho; resultados por pergunta; esforço; ameaças à validade; implicações práticas. Entrega: manuscrito em inglês com cada conclusão ligada a evidência, incluindo resultados desfavoráveis.
- [ ] **I10 · P1 · Preparar o pacote de submissão.** Código, configurações, manifesto dos dados, instruções, logs publicáveis e limitações; conferir o guia vigente, template, declarações e arquivos editoriais exigidos. Entrega: manuscrito e artefato revisados internamente; nenhuma submissão automática.

### Critério interno para avançar

Processo executável, referências resolvidas, comparação com controles competitivos, evidência em revisões reais de mais de um projeto, avaliação verificável e esforço humano medido. O número de projetos e casos deve ser justificado; estes critérios são recomendações para o desenho, não requisitos quantitativos oficiais do IST. Sem esses elementos, o produto continua uma prova de conceito.

Uma validação com desenvolvedores externos pode fortalecer as conclusões sobre utilidade e usabilidade, se essa for uma alegação do artigo. Ela não é apresentada aqui como obrigação universal para submissão ao IST.

## 5. P1 — artigo de IA, alvo recomendado TMLR

Título de trabalho: **Contextualized Experience Memory under Task Shift: Transfer, Retention, and Interference in Frozen LLM Agents**. A pergunta permanece candidata até terminar C04–C05.

### Lacuna que precisa ser demonstrada

Hu, Long e Wang já investigam a interação entre representação, organização e recuperação de experiências em aprendizagem sequencial com memória externa. Portanto, mostrar apenas que memória sofre interferência, ou que regras abstraídas podem transferir melhor que trajetórias, não é uma contribuição distintiva suficiente por si só. [When Continual Learning Moves to Memory, preprint de 2026](https://arxiv.org/abs/2604.27003).

Uma direção a investigar é a validade contextual das regras: como identificar quando uma experiência ainda se aplica, quando uma mudança de contexto a invalida e quando deve coexistir com uma regra aparentemente conflitante. Isso não é uma alegação de novidade verificada. Outra rota legítima para TMLR é um estudo explicativo rigoroso de uma questão ainda não resolvida, sem propor algoritmo novo.

### TODO

- [ ] **A01 · P0 · Decidir o objeto científico.** Escolher entre estudo explicativo e política de memória, com base na matriz C04. Escrever que pergunta nova o trabalho responde para pesquisadores de aprendizagem, além da engenharia de software. Entrega: diferencial demonstrável; depende C04–C05.
- [ ] **A02 · P1 · Formalizar o estado que muda.** Descrever tarefa x_t, memória M_t, recuperação r, orçamento B e atualização U, com pesos theta fixos. Distinguir orçamento de armazenamento e orçamento de contexto recuperado. Entrega: definição operacional e pseudocódigo, suficientes para reproduzir o método.
- [ ] **A03 · P1 · Construir sequências diagnósticas.** Incluir experiências compatíveis, distratoras e conflitantes; mudanças de contexto com aplicabilidade conhecida; e versões limpas/ruidosas da memória. Ruído artificial deve ter intensidade declarada e, se possível, relação com erros observados no piloto. Entrega: sequências separadas para desenvolvimento e avaliação final.
- [ ] **A04 · P1 · Escolher tarefas com generalização útil.** Adicionar um ambiente além de code review, por exemplo tarefas verificáveis de SQL ou outro ambiente reproduzível do LifelongAgentBench, após testar viabilidade. A diversidade é recomendação para a amplitude da conclusão, não uma regra editorial fixa. Entrega: tarefas com critérios de sucesso documentados e escopo de alegações correspondente.
- [ ] **A05 · P1 · Implementar baselines pertinentes.** Sem memória; estática; últimos episódios; recuperação por similaridade; regra resumida sem validação de aplicabilidade; e método próximo reproduzível de C04. Reflexion/A-MEM entram se seu mecanismo e custo forem comparáveis; adaptar e declarar adaptações. Entrega: tabela de acesso a dados, feedback e orçamento por método.
- [ ] **A06 · P1 · Isolar os fatores em ablações.** Representação episódica versus abstrata; regra com/sem escopo; com/sem tratamento de conflito; seleção versus reescrita. Manter feedback e evidência de entrada iguais quando a pergunta exigir comparação causal. Entrega: análise que explique de onde vem o efeito, mesmo se não houver ganho.
- [ ] **A07 · P1 · Avaliar estabilidade e plasticidade.** Medir ganho nas famílias novas, retenção nas anteriores, transferência negativa, recuperação de memórias inadequadas e recuperação após mudança de contexto. Entrega: matriz tarefa–checkpoint e curvas por sequência, com orçamento controlado.
- [ ] **A08 · P1 · Testar robustez do entendimento.** Repetir em ordens distintas e em mais de um modelo quando a conclusão visar independência de backend. Começar com um modelo no piloto e ampliar conforme o custo. Registrar limites de generalização se um segundo modelo ou ambiente não for viável. Entrega: resultado que não dependa apenas de uma sequência escolhida.
- [ ] **A09 · P1 · Explicar os limites.** Relacionar perdas a escopo errado, recuperação, conflito, ruído ou limite de contexto, com intervenções controladas. Não atribuir automaticamente regressões a esquecimento paramétrico. Entrega: exemplos e ablações que apoiem a interpretação, em vez de apenas uma tabela agregada.
- [ ] **A10 · P1 · Escrever resultados exclusivos para IA.** Formalização; mecanismos; avaliação entre contextos; ablações; análise de memória e limites. Se EF/QIP for removido da introdução, a contribuição de aprendizagem deve continuar compreensível. Entrega: manuscrito que se sustente independentemente do artigo IST.
- [ ] **A11 · P1 · Fazer a conferência editorial final.** Conferir anonimização, template, perfis OpenReview, quotas dos autores e políticas vigentes. Entrega: pacote de submissão revisado; nenhuma submissão automática. [Orientações aos autores](https://jmlr.org/tmlr/author-guide.html); [políticas](https://jmlr.org/tmlr/editorial-policies.html).

### Critério interno para avançar

Pergunta distinta dos trabalhos próximos, evidência que suporte a amplitude da conclusão, baselines relevantes, ablações e análise de transferência/retenção. Um algoritmo novo não é condição universal para TMLR; entendimento novo e confiável sobre um método existente pode satisfazer os critérios. Se restar somente o sistema de revisão com curadoria e seus resultados, fortalecer o IST em vez de duplicar o artigo.

## 6. Separação entre as publicações

A política atual da TMLR veda reutilização de texto, figuras ou resultados de trabalho publicado, aceito ou submetido em paralelo a outra venue archival com revisão por pares. Isso afeta diretamente a estratégia de dois artigos. [Política oficial: Dual Submissions and Originality](https://jmlr.org/tmlr/editorial-policies.html).

- [ ] **S01 · P0 · Criar um mapa de propriedade da evidência.** Designar quais execuções, tabelas, figuras e conclusões pertencem a IST e quais pertencem a IA, antes da escrita.
- [ ] **S02 · P1 · Compartilhar infraestrutura com rastreabilidade.** O mesmo código-base e fontes públicas podem apoiar estudos diferentes; reutilização de ativos não autoriza republicar a mesma evidência. Documentar versões e origem dos dados.
- [ ] **S03 · P1 · Separar execuções científicas.** IST usa estudo de processo e revisão; IA usa novas execuções sobre mecanismos, mudança de contexto e generalização. Novas execuções por si só não bastam se a pergunta e a contribuição continuarem as mesmas.
- [ ] **S04 · P1 · Relacionar os trabalhos com transparência.** Citar o trabalho relacionado quando aplicável e declarar material concomitante conforme as regras editoriais e de anonimização. Se houver dúvida sobre sobreposição, preparar consulta editorial com exemplos concretos antes de submeter; não enviar mensagem sem autorização do autor.
- [ ] **S05 · P1 · Verificar o status do SBES.** A nota registra recusa. Obter o documento e confirmar eventual preprint, publicação posterior ou outra submissão; não presumir publicação archival nem inferir o motivo da recusa sem ler os pareceres.

## 7. Ordem de execução respeitando o mês do TCC

O prazo de um mês e o uso limitado de API foram informados anteriormente nesta conversa. Este plano preserva essa restrição para a prova de conceito; não assume que dois artigos completos, aprovados ou sequer prontos para submissão cabem no mesmo período.

| Etapa relativa | Prioridade | Entrega e decisão |
|---|---|---|
| Semana 1 | C01–C07, C11–C12 e S01 | Inventário, literatura próxima, fronteiras dos artigos, protocolo e piloto; decidir se o segundo artigo tem pergunta própria |
| Semana 2 | C08–C10 e preparação I01–I04 | Infraestrutura, quatro condições e avaliação isolada funcionando |
| Semana 3 | Execução do núcleo viável | Resultados da prova de conceito, custo e análise de erros; preservar conjuntos futuros de publicação |
| Semana 4 | TCC e decisão de expansão | TCC com evidência limitada declarada, pacote reproduzível e plano dimensionado do IST |
| Após a prova de conceito | I05–I10 | Ampliar e concluir o artigo IST conforme as evidências faltantes |
| Em frente de pesquisa distinta | A01–A11 | Produzir a avaliação própria para IA; reavaliar o alvo se não houver uma questão generalizável |

Não fixar uma data de submissão à custa de retirar controles ou usar o teste final para desenvolvimento. Não estimar preço total apenas pelo número de chamadas: o custo depende de tokens, modelo, testes e repetições.

**Próxima ação concreta:** reunir manuscrito/pareceres e executar C04 em paralelo ao planejamento do piloto, sem iniciar experimentos caros. O IST está mais próximo do material existente; a frente de IA depende primeiro de esclarecer o que se pode aprender além dos estudos recentes de memória.

## 8. Leituras que entram na matriz C04

| Fonte | O que extrair para o TODO |
|---|---|
| [Basili e Caldiera — The Experience Factory Strategy and Practice, 1995](https://drum.lib.umd.edu/items/92069f0c-6fc8-4ddc-aa3b-2e5cafdaac30) | Ciclos, artefatos e compromissos da adaptação EF/QIP |
| [Zheng et al. — Lifelong Learning of Large Language Model Based Agents: A Roadmap](https://arxiv.org/abs/2501.07278) | Definições e limites de memória/aprendizagem; usar a versão publicada quando adequada |
| [Shinn et al. — Reflexion, NeurIPS 2023](https://proceedings.neurips.cc/paper_files/paper/2023/hash/1b44b878bb782e6954cd888628510e90-Abstract-Conference.html) | Feedback linguístico, buffer episódico e diferenças entre retentativa e transferência |
| [Xu et al. — A-MEM, preprint 2025](https://arxiv.org/abs/2502.12110) | Organização e atualização de memórias; conferir versão e implementação antes de adotar como baseline |
| [Zheng et al. — LifelongAgentBench, preprint 2025](https://arxiv.org/abs/2505.11942) | Protocolo sequencial e ambientes verificáveis |
| [Lopez-Paz e Ranzato — Gradient Episodic Memory, NeurIPS 2017](https://proceedings.neurips.cc/paper/2017/hash/f87522788a2be2d171666752f97ddebb-Abstract.html) | Definições de avaliação de transferência, sem pressupor uso do algoritmo GEM |
| [Xiong et al. — How Memory Management Impacts LLM Agents, ACL 2026](https://aclanthology.org/2026.acl-long.27/) | Experiências ruins e replay inadequado; não converter motivação de qualidade em obrigação de curadoria humana |
| [Hu, Long e Wang — When Continual Learning Moves to Memory, preprint 2026](https://arxiv.org/abs/2604.27003) | Sobreposição direta com representação/recuperação e transferência/retenção; leitura prioritária |
| [Zhang et al. — Code Review Agent Benchmark, preprint 2026](https://arxiv.org/abs/2603.23448) | Viabilidade e limitações do c-CRAB; dependência do agente corretor |

Verificação desta rodada: políticas e critérios TMLR consultados em páginas oficiais; escopo IST confirmado na página oficial da editora. O guia detalhado IST no ScienceDirect respondeu 403; requisitos finos de submissão devem ser conferidos no portal oficial acessível aos autores. A lista de leituras não atesta leitura integral nem cobertura exaustiva da literatura.
