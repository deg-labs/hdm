<!-- START doctoc generated TOC please keep comment here to allow auto update -->
<!-- DON'T EDIT THIS SECTION, INSTEAD RE-RUN doctoc TO UPDATE -->
**Table of Contents**  *generated with [DocToc](https://github.com/thlorenz/doctoc)*

- [リリース / デプロイ方法](#%E3%83%AA%E3%83%AA%E3%83%BC%E3%82%B9--%E3%83%87%E3%83%97%E3%83%AD%E3%82%A4%E6%96%B9%E6%B3%95)

<!-- END doctoc generated TOC please keep comment here to allow auto update -->


## リリース / デプロイ方法

このリポジトリは GitHub Actions の `deploy-to-gce` ワークフローで GCE インスタンスへデプロイされます。

1. `main` ブランチから作業ブランチを作成し、変更を実施します。
2. `main` 向けにプルリクエストを作成します。
3. デプロイ先に応じて PR にラベルを付与します。
   - `dev`：開発環境へデプロイ
   - `stg`：ステージング環境へデプロイ
   - `prd`：本番環境へデプロイ
4. ラベル付与をトリガーにワークフローが実行され、Workload Identity Federation で認証後にインスタンスへ `docker compose` でデプロイされます。
5. 動作確認後、`main` へマージしてリリースを完了します。

