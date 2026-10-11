{{- define "storefront.labels" -}}
app: {{ .app }}
app.kubernetes.io/part-of: storefront
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
{{- end }}

{{- define "storefront.image" -}}
{{- $override := .override | default dict -}}
{{- $repo := $override.repository | default .root.Values.image.repository -}}
{{- $tag := $override.tag | default .root.Values.image.tag -}}
{{- printf "%s:%s" $repo $tag -}}
{{- end }}

{{- define "storefront.env" -}}
{{- range $name, $value := . }}
- name: {{ $name }}
  value: {{ $value | quote }}
{{- end }}
{{- end }}
