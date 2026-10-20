---
title: "Étude de faisabilité — assignation dynamique d'un Foundation Model à un agent existant"
date: 2026-08-12
author: Mostapha EL ANSARI
tags:
  - aws
  - bedrock
  - agentcore
  - harness
  - runtime
  - model-routing
status: technical-study
---

# Étude de faisabilité — assignation dynamique d'un Foundation Model à un agent existant

> [!summary]
> **Oui, l'assignation dynamique est techniquement possible dans AgentCore, sans
> recréer l'agent.** Pour un **Managed AgentCore Harness**, le champ
> `model.bedrockModelConfig.modelId` de `InvokeHarness` remplace le modèle par
> défaut uniquement pendant l'invocation courante. Pour un **Custom AgentCore
> Runtime**, le payload d'invocation est libre : le code de l'agent doit lire un
> tier ou un identifiant autorisé et construire le client du modèle
> correspondant. Si cette capacité n'existe pas déjà dans l'image du Runtime,
> un premier redéploiement est nécessaire pour l'ajouter. Les **Agents for
> Amazon Bedrock classiques** suivent un autre mécanisme : `UpdateAgent`,
> préparation, version et alias ; ce n'est pas un override par requête.

## Question étudiée

L'objectif est de déterminer si une application peut prendre un agent AWS déjà
créé et lui assigner un Foundation Model différent selon la requête, sans :

- recréer l'agent ;
- déployer un agent distinct pour chaque modèle ;
- modifier sa configuration persistante à chaque requête ;
- perdre les outils, le prompt système ou le contexte de session existants.

Le terme « agent » peut désigner plusieurs ressources AWS. La réponse dépend
donc du produit exact.

## Conclusion de faisabilité

| Type d'agent | Changement par invocation | Redéploiement requis | Modification persistante | Verdict |
|---|---:|---:|---:|---|
| **Managed AgentCore Harness** | Oui, via `InvokeHarness.model` | Non | Non | **Support natif et recommandé** |
| **Custom AgentCore Runtime déjà compatible** | Oui, via le payload puis le code de l'agent | Non | Non | **Possible et déjà implémenté dans ce dépôt** |
| **Custom Runtime non compatible** | Non, pas encore | Une fois pour ajouter le sélecteur | Oui, nouvelle version du Runtime | **Possible après adaptation** |
| **Agents for Amazon Bedrock classiques** | Non via `InvokeAgent` | Préparation/version nécessaires | Oui | **Pas adapté au routage par requête** |

La solution recommandée dans ce projet est donc :

```text
Décision prise par l'application
             │
             ├── Managed Harness
             │      └── InvokeHarness(modelId=<modèle choisi>)
             │
             └── Custom Runtime
                    └── InvokeAgentRuntime({model_tier: <tier choisi>})
```

La décision peut provenir d'un choix utilisateur, d'une politique statique,
d'un routeur NLP ou d'un autre mécanisme. Le mécanisme d'assignation reste le
même.

## 1. Managed AgentCore Harness

### 1.1 Modèle par défaut et override temporaire

Un Harness possède un modèle par défaut dans sa configuration. Ce défaut est
utilisé lorsqu'un appel ne fournit pas de champ `model`.

Le contrat officiel de `InvokeHarness` accepte toutefois un objet `model`
optionnel. AWS le décrit explicitement comme la configuration utilisée pour
l'invocation et précise qu'elle remplace le défaut du Harness lorsqu'elle est
présente. Le type `HarnessBedrockModelConfig` impose un `modelId` et accepte
également `maxTokens`, `temperature`, `topP`, `apiFormat` et des paramètres
additionnels.

Documentation AWS :

- [API InvokeHarness](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeHarness.html)
- [HarnessBedrockModelConfig](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_HarnessBedrockModelConfig.html)
- [Models and instructions — override par invocation](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-models.html)

Exemple minimal :

```python
response = client.invoke_harness(
    harnessArn=existing_harness_arn,
    runtimeSessionId=session_id,
    actorId=actor_id,
    messages=[
        {
            "role": "user",
            "content": [{"text": user_prompt}],
        }
    ],
    model={
        "bedrockModelConfig": {
            "modelId": selected_model_id,
            "maxTokens": 4096,
        }
    },
)
```

Effet de cet appel :

```text
Configuration persistante du Harness : Sonnet

Invocation A sans override       -> Sonnet
Invocation B modelId=Haiku       -> Haiku
Invocation C sans override       -> Sonnet
```

L'invocation B ne modifie ni la ressource Harness, ni son modèle par défaut, ni
sa version. Il n'y a donc pas de déploiement entre B et C.

### 1.2 Agent préexistant

L'override ne dépend pas de la création du Harness. Il suffit de connaître
l'ARN du Harness existant et d'avoir le droit de l'invoquer. On ne crée pas une
seconde ressource et on ne lance pas `UpdateHarness`.

AWS indique également qu'un Harness peut changer de modèle ou de fournisseur
entre les tours d'une même session tout en conservant le contexte. Cela signifie
que deux appels successifs utilisant le même `runtimeSessionId` peuvent fournir
deux configurations de modèle différentes.

Il faut néanmoins distinguer :

- **entre deux appels `InvokeHarness`** : le modèle peut changer ;
- **dans une invocation unique** : le champ `model` définit le modèle utilisé
  par la boucle de cette invocation ; l'API n'expose pas un changement de modèle
  à chaque cycle interne outil/modèle.

Pour une orchestration « planifier avec A puis exécuter avec B », l'application
peut donc séparer le travail en deux invocations. Si le changement doit se faire
à l'intérieur d'une seule boucle, un Runtime avec orchestration personnalisée
est plus approprié.

### 1.3 Implémentation existante dans ce dépôt

La fonctionnalité est déjà câblée dans l'application de comparaison.

#### Décision partagée

`apps/comparison/services/run_controller.py` enrichit les options avant de
lancer les deux architectures :

```python
self.options = routed_options(prompt, options)
```

Le résultat contient notamment :

```json
{
  "model_tier": "cheap",
  "model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "model_route_reason": "routine request"
}
```

#### Mapping du tier Managed

`apps/comparison/backends/harness.py` transforme le tier logique en identifiant
de modèle autorisé :

```python
def _selected_model_id(options: dict) -> str:
    tier = options.get("model_tier")
    if tier == "cheap":
        return HARNESS_CHEAP_MODEL_ID
    if tier == "strong":
        return HARNESS_MODEL_ID
    return options.get("model_id") or HARNESS_MODEL_ID
```

Puis `_invocation_overrides` construit la partie AWS :

```python
return {
    "model": {
        "bedrockModelConfig": {
            "modelId": _selected_model_id(options),
            "maxTokens": HARNESS_MAX_TOKENS,
        }
    }
}
```

Enfin, cet objet est injecté dans l'appel existant :

```python
response = client.invoke_harness(
    harnessArn=config["arn"],
    runtimeSessionId=session_id,
    actorId=ACTOR_ID,
    messages=[...],
    **_invocation_overrides(client, options),
)
```

Le modèle est donc affecté à l'invocation, pas au Harness de manière
permanente.

### 1.4 Vérification locale du contrat SDK

Le modèle de service botocore installé dans l'environnement du projet a été
inspecté le 12 août 2026. Il confirme que :

- `InvokeHarness.model` est optionnel ;
- `model` est une union entre Bedrock, Gemini, LiteLLM et OpenAI ;
- `bedrockModelConfig.modelId` est obligatoire lorsque la branche Bedrock est
  utilisée ;
- `additionalParams`, `apiFormat`, `maxTokens`, `temperature` et `topP` sont
  disponibles.

Cette vérification locale correspond au contrat publié dans la documentation
AWS. Elle évite de dépendre uniquement d'un exemple applicatif.

## 2. Changer durablement le modèle par défaut du Harness

Un override dynamique ne doit pas être confondu avec un changement de défaut.

Pour rendre un modèle permanent, il faut utiliser `UpdateHarness` ou modifier la
configuration IaC. AgentCore crée alors une nouvelle version immuable du
Harness. Le endpoint `DEFAULT` suit automatiquement la version la plus récente,
tandis qu'un endpoint nommé et épinglé reste sur sa version jusqu'à une mise à
jour explicite.

[La documentation AWS sur les versions et endpoints Harness](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-versioning.html)
décrit ce comportement.

| Besoin | Mécanisme |
|---|---|
| Choisir un modèle pour une requête | `InvokeHarness.model` |
| Faire un test A/B ponctuel | `InvokeHarness.model` |
| Changer le modèle par défaut | `UpdateHarness` |
| Déployer progressivement une nouvelle configuration complète | Nouvelle version + endpoint nommé |
| Revenir immédiatement à une configuration complète antérieure | Repointage de l'endpoint vers une ancienne version |

Le routage par requête doit rester dans le data plane (`InvokeHarness`). Utiliser
`UpdateHarness` pour chaque requête créerait inutilement des versions, ajouterait
de la latence de contrôle et introduirait des courses entre utilisateurs.

## 3. Custom AgentCore Runtime

### 3.1 Différence de contrat

`InvokeAgentRuntime` reçoit un payload binaire pouvant contenir le JSON défini
par l'application. Contrairement à `InvokeHarness`, l'API AgentCore Runtime ne
propose pas un champ Foundation Model standard : c'est le code du conteneur qui
interprète le payload et appelle Bedrock.

[Documentation AWS — Invoke an AgentCore Runtime agent](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)

Deux situations existent donc :

1. Le Runtime sait déjà choisir un modèle : le choix est dynamique immédiatement.
2. Le Runtime a un modèle codé en dur : il faut déployer une nouvelle version
   qui introduit le sélecteur ; les invocations suivantes deviennent dynamiques.

### 3.2 Implémentation présente dans ce dépôt

Le client envoie :

```json
{
  "prompt": "...",
  "model_tier": "cheap",
  "model_route_reason": "routine request"
}
```

Dans `agents/custom/agent.py`, le Runtime résout le tier :

```python
requested_model_tier = str(payload.get("model_tier") or "strong").strip().lower()
model_tier = "cheap" if requested_model_tier == "cheap" else "strong"
model_id = CHEAP_MODEL_ID if model_tier == "cheap" else MODEL_ID
```

Le modèle est ensuite injecté dans `BedrockModel` lors de la construction de
l'agent pour cette invocation. Les deux valeurs proviennent de l'environnement
du Runtime :

```text
BEDROCK_CHEAP_MODEL_ID
BEDROCK_MODEL_ID
```

Cette approche évite de transformer le Runtime en proxy permettant à un appelant
de fournir n'importe quel ARN. Le payload contient un tier, puis le conteneur le
mappe vers un catalogue contrôlé.

## 4. Cas différent : Agents for Amazon Bedrock classiques

Pour un Agent Bedrock classique, `foundationModel` fait partie de la
configuration de l'agent. L'API `UpdateAgent` permet de changer ce champ dans le
working draft. La mise en production passe ensuite par la préparation, une
version et un alias.

- [API UpdateAgent](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_UpdateAgent.html)
- [Déploiement, versions et aliases d'un Agent Bedrock](https://docs.aws.amazon.com/bedrock/latest/userguide/deploy-agent.html)

Ce mécanisme répond à « changer la configuration de l'agent », mais pas à
« choisir un modèle pour chaque requête ». Les versions sont immuables et les
applications invoquent un alias associé à une version. Pour exposer plusieurs
modèles simultanément, il faudrait préparer plusieurs versions/agents et router
vers leurs aliases, avec plus de gestion opérationnelle.

## 5. Prérequis techniques

### 5.1 Disponibilité régionale

Le modèle ou son inference profile doit être invocable depuis la région où le
Harness ou Runtime effectue l'appel. Ce projet utilise des inference profiles EU
afin de conserver le routage dans la géographie européenne :

```text
eu.anthropic.claude-haiku-4-5-20251001-v1:0
eu.anthropic.claude-sonnet-4-6
```

Bedrock accepte un ID ou ARN d'inference profile dans `modelId`. Un profil
cross-Region peut répartir l'inférence sur plusieurs régions de la géographie.
Les politiques IAM et SCP doivent autoriser toutes les régions de destination du
profil, sinon l'invocation peut échouer même si une autre destination est
disponible.

- [Utiliser un inference profile](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-use.html)
- [Régions et modèles supportés par les inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-support.html)

### 5.2 Autorisations IAM

Deux identités sont impliquées :

- l'appelant doit pouvoir invoquer le Harness ou le Runtime ;
- le rôle d'exécution de l'agent doit pouvoir invoquer les modèles Bedrock
  sélectionnés.

Lorsqu'un inference profile est utilisé, la politique doit couvrir le profil et
les Foundation Models des régions qui lui sont associées. AWS documente
notamment `bedrock:InvokeModel*` et la possibilité de restreindre l'accès via la
condition `aws:InferenceProfileArn`.

[Prérequis IAM des inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html)

Le rôle doit être limité au catalogue de modèles réellement autorisés. Le
routeur applicatif ne doit pas recopier directement un `modelId` libre fourni
par l'utilisateur.

### 5.3 Compatibilité fonctionnelle

Deux modèles textuels ne sont pas nécessairement interchangeables. Avant de les
placer dans le même catalogue dynamique, il faut vérifier :

- support de l'API choisie (`converse_stream`, Responses ou Chat Completions) ;
- support du tool use et format des appels d'outils ;
- tailles de contexte et de sortie ;
- paramètres acceptés, notamment thinking et prompt caching ;
- modalités supportées pour les fichiers ou images ;
- disponibilité et quotas dans la géographie requise.

Un modèle non compatible doit être rejeté avant l'invocation ou exclu du
catalogue. La possibilité de transmettre un `modelId` ne garantit pas que le
modèle possède les capacités exigées par l'agent.

### 5.4 Session et cache

Le Managed Harness conserve son contexte de session lorsque le modèle change
entre deux tours, conformément à la documentation AWS. Il faut toutefois tester
la qualité de la transition : le nouveau modèle doit comprendre l'historique et
les résultats d'outils produits par le précédent.

Le prompt caching doit également être mesuré séparément après un changement de
modèle. Un changement de cible ne doit pas être considéré comme un cache hit
garanti ; seules les métriques réelles de lecture/écriture de cache doivent être
utilisées.

## 6. Contrat d'assignation recommandé

L'application devrait exposer un contrat logique, indépendant des IDs AWS :

```python
MODEL_CATALOG = {
    "cheap": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
    "strong": "eu.anthropic.claude-sonnet-4-6",
}


def resolve_model(tier: str) -> str:
    try:
        return MODEL_CATALOG[tier]
    except KeyError as exc:
        raise ValueError("unsupported model tier") from exc
```

La décision normalisée contient :

```json
{
  "requested_tier": "auto",
  "selected_tier": "cheap",
  "selected_model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
  "route_reason": "routine request",
  "router_version": "router-v1"
}
```

Puis chaque adapter traduit ce contrat :

```text
Managed -> model.bedrockModelConfig.modelId
Custom  -> payload.model_tier -> mapping interne du Runtime
```

Ce découplage permet d'avoir des IDs différents par région tout en conservant
les mêmes tiers métier.

## 7. Observabilité requise

Chaque invocation doit enregistrer au minimum :

```text
harness/runtime ARN
session ID
requested tier
selected tier
actual model ID
route reason
router version
input/output/cache tokens
latency
tool calls
error or success
```

Le modèle affiché doit venir de l'override réellement résolu ou des métadonnées
retournées par le Runtime, pas seulement de la préférence demandée par le
client. Ce dépôt ajoute déjà `model_id`, `model_tier` et
`model_route_reason` aux événements de métriques normalisés.

## 8. Protocole de preuve sur un agent existant

Le test de faisabilité peut être exécuté sans modifier la ressource Harness.

### Étape 1 — Capturer l'état initial

```text
Harness ARN
version courante
endpoint/qualifier
modèle par défaut
rôle d'exécution
```

### Étape 2 — Invoquer avec le modèle économique

```python
invoke_harness(
    harnessArn=arn,
    runtimeSessionId=session_id,
    messages=[...],
    model={
        "bedrockModelConfig": {
            "modelId": HAIKU_MODEL_ID,
            "maxTokens": 4096,
        }
    },
)
```

### Étape 3 — Invoquer avec le modèle fort

Utiliser le même ARN, puis fournir `SONNET_MODEL_ID`. Un second essai peut
réutiliser le même `runtimeSessionId` afin de vérifier le changement entre deux
tours et la conservation du contexte.

### Étape 4 — Vérifier les preuves

- les deux invocations réussissent ;
- les traces identifient les modèles réellement utilisés ;
- la version et le modèle par défaut du Harness n'ont pas changé ;
- l'invocation suivante sans override revient au modèle par défaut ;
- outils, prompt, contexte et streaming fonctionnent avec les deux modèles ;
- coûts et tokens sont attribués au bon modèle.

### Étape 5 — Tests négatifs

- modèle non autorisé par IAM ;
- modèle indisponible dans la région ;
- modèle ne supportant pas l'API ou le tool use ;
- tier inconnu ;
- absence d'override ;
- changement de modèle entre deux tours d'une même session.

## 9. Risques et mesures

| Risque | Effet | Mesure proposée |
|---|---|---|
| `modelId` arbitraire fourni par le client | Contournement du catalogue et coûts non maîtrisés | Mapping tier vers allowlist côté serveur |
| Permission manquante pour le second modèle | `AccessDeniedException` | Test IAM préalable et déploiement de la policy avec le catalogue |
| Modèle incompatible avec les outils | Échec ou mauvaise trajectoire d'agent | Matrice de capacités et tests par modèle |
| Changement du modèle par défaut par erreur | Impact sur tout le trafic | Utiliser `InvokeHarness.model`, pas `UpdateHarness` |
| Course entre utilisateurs avec un changement de contrôle plane | Configuration non déterministe | Aucun `UpdateHarness` dans le chemin d'une requête |
| Modèle demandé différent du modèle réellement utilisé | Mesures de coût fausses | Retourner et tracer le modèle résolu |
| Transition de modèle dans une session | Qualité de contexte variable | Test multi-tour et évaluation dédiée |
| Inference profile bloqué par SCP | Échec complet du profil | Autoriser toutes les destinations requises |

## 10. Recommandation finale

Pour le **Managed AgentCore Harness préexistant**, utiliser l'override
`InvokeHarness.model.bedrockModelConfig.modelId`. C'est la capacité native
prévue par AWS pour les expériences, comparaisons prix/performance et changements
de modèle par invocation. Elle ne nécessite ni recréation ni redéploiement du
Harness.

Pour le **Custom AgentCore Runtime**, conserver le contrat actuel basé sur
`model_tier`, résolu dans le conteneur vers deux IDs autorisés. Cette solution est
dynamique après le premier déploiement de l'image compatible.

Réserver `UpdateHarness` ou `UpdateAgent` aux changements persistants et
versionnés. Ces APIs ne doivent pas être utilisées comme moteur de routage dans
le chemin temps réel d'une requête.

## Références principales

- [AWS — InvokeHarness API](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_InvokeHarness.html)
- [AWS — Harness model configuration](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-models.html)
- [AWS — HarnessBedrockModelConfig](https://docs.aws.amazon.com/bedrock-agentcore/latest/APIReference/API_HarnessBedrockModelConfig.html)
- [AWS — AgentCore Harness versioning and endpoints](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/harness-versioning.html)
- [AWS — Invoke an AgentCore Runtime agent](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-invoke-agent.html)
- [AWS — UpdateAgent](https://docs.aws.amazon.com/bedrock/latest/APIReference/API_agent_UpdateAgent.html)
- [AWS — Bedrock Agent versions and aliases](https://docs.aws.amazon.com/bedrock/latest/userguide/deploy-agent.html)
- [AWS — Inference profiles](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-use.html)
