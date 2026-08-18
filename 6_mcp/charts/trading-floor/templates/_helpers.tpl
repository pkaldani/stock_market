{{/*
Chart name and version, used for the standard "helm.sh/chart" label.
*/}}
{{- define "trading-floor.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Base name for release resources.
*/}}
{{- define "trading-floor.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{/*
Common labels applied to every resource.
*/}}
{{- define "trading-floor.labels" -}}
helm.sh/chart: {{ include "trading-floor.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: trading-floor
{{- end -}}

{{/*
Selector labels for a given component ("app" or "frontend").
*/}}
{{- define "trading-floor.selectorLabels" -}}
app.kubernetes.io/name: {{ include "trading-floor.fullname" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Full image reference for a component. Args: (dict "root" . "repoOverride" .. "tagOverride" .. "suffix" ..)
Falls back to `image.repository + suffix` and `image.tag | Chart.AppVersion` when the
per-component override is unset.
*/}}
{{- define "trading-floor.image" -}}
{{- $root := .root -}}
{{- $repo := .repoOverride | default (printf "%s-%s" $root.Values.image.repository .suffix) -}}
{{- $tag := .tagOverride | default ($root.Values.image.tag | default $root.Chart.AppVersion) -}}
{{- if $root.Values.image.registry -}}
{{- printf "%s/%s:%s" $root.Values.image.registry $repo $tag -}}
{{- else -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end -}}
{{- end -}}

{{/*
Name of the Secret holding provider/API credentials — either the user-supplied
existingSecret or the one this chart creates.
*/}}
{{- define "trading-floor.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else -}}
{{- printf "%s-secrets" (include "trading-floor.fullname" .) -}}
{{- end -}}
{{- end -}}

{{/*
Name of the PVC holding accounts.db and memory/ — either the user-supplied
existingClaim or the one this chart creates.
*/}}
{{- define "trading-floor.pvcName" -}}
{{- if .Values.persistence.existingClaim -}}
{{- .Values.persistence.existingClaim -}}
{{- else -}}
{{- printf "%s-data" (include "trading-floor.fullname" .) -}}
{{- end -}}
{{- end -}}
